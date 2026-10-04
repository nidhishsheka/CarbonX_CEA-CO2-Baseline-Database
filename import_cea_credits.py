import hashlib, shutil, sqlite3, sys
from datetime import datetime
from openpyxl import load_workbook

DB = sys.argv[1] if len(sys.argv) > 1 else "cc.db"
XLSX = sys.argv[2] if len(sys.argv) > 2 else "Baseline_Carbon_Dioxide_Emission_Database_Version_22_0.xlsx"
BASE_END, PERIOD_END = 2024, 2026           # fiscal-year end years
FY = ["2017-18", "2018-19", "2019-20", "2020-21", "2021-22", "2022-23", "2023-24", "2024-25", "2025-26"]
col = lambda end: 11 + 5 * (end - 2018) + 1  # column of absolute CO2 (t) for FY ending `end`

shutil.copy2(DB, f"{DB}.bak_{datetime.now():%Y%m%d_%H%M%S}")
conn = sqlite3.connect(DB)
sha = hashlib.sha256(open(XLSX, "rb").read()).hexdigest()

row = conn.execute("SELECT provenance_id FROM Data_Provenance WHERE dataset_name LIKE 'CEA CO2 Baseline%' "
                   "ORDER BY provenance_id DESC LIMIT 1").fetchone()
if row:
    prov = row[0]
else:
    prov = conn.execute("""INSERT INTO Data_Provenance (dataset_name, organization, dataset_version, source_url,
        license, retrieval_date, methodology, is_synthetic) VALUES (?,?,?,?,?,?,?,0)""",
        ("CEA CO2 Baseline Database for the Indian Power Sector",
         "Central Electricity Authority, Ministry of Power, Government of India", "22.0 (August 2026)",
         "https://cea.nic.in", "Confirm terms of use with CEA", datetime.now().date().isoformat(),
         f"File SHA-256: {sha}")).lastrowid

# reporting source + factor 1.0 (the CO2 is already measured, so no conversion is applied)
src = conn.execute("SELECT source_id FROM Emission_Source WHERE source_name='CEA Reported CO2'").fetchone()
if src:
    src = src[0]
else:
    src = conn.execute("INSERT INTO Emission_Source (source_name, unit) VALUES ('CEA Reported CO2','kg')").lastrowid
    conn.execute("INSERT INTO Emission_Factor (source_id, factor_value, effective_year, is_active, provenance_id) "
                 "VALUES (?,1.0,2017,1,?)", (src, prov))
if not conn.execute("SELECT 1 FROM Reduction_Policy WHERE sector='Energy'").fetchone():
    conn.execute("INSERT INTO Reduction_Policy (sector, reduction_percent, effective_year) VALUES ('Energy',15,2025)")

ws = load_workbook(XLSX, read_only=True, data_only=True)["Data"]
plants = [r for r in ws.iter_rows(min_row=2, values_only=True)
          if r[1] and isinstance(r[2], (int, float)) and r[2] == 0]
cb, cp = col(BASE_END), col(PERIOD_END)

existing = {r[0] for r in conn.execute("SELECT est_name FROM Establishment")}
added = skipped = 0
for r in plants:
    base_t, act_t = r[cb], r[cp]
    if not (isinstance(base_t, (int, float)) and isinstance(act_t, (int, float)) and base_t > 0 and act_t > 0):
        skipped += 1
        continue
    name = str(r[1]).strip()
    if name in existing:                      # already imported (or name clash) -> make unique & idempotent
        if conn.execute("""SELECT 1 FROM Establishment e JOIN Baseline_Emission b ON b.est_id=e.est_id
                           WHERE e.est_name=? AND b.provenance_id=?""", (name, prov)).fetchone():
            continue
        name = f"{name} (CEA #{int(r[0])})"
    est = conn.execute("INSERT INTO Establishment (est_name, est_type, location) VALUES (?,?,?)",
                       (name, "Energy", (r[5] or "").title())).lastrowid
    existing.add(name)
    conn.execute("INSERT INTO Baseline_Emission (est_id, baseline_year, baseline_emission_kg, provenance_id) "
                 "VALUES (?,?,?,?)", (est, BASE_END, base_t * 1000, prov))
    conn.execute("INSERT OR IGNORE INTO Carbon_Credit (est_id, credit, status) VALUES (?,0,'Neutral')", (est,))
    conn.execute("INSERT OR IGNORE INTO Credit_Wallet (est_id, available_credit, reserved_credit, generated_credit) "
                 "VALUES (?,0,0,0)", (est,))
    conn.execute("INSERT INTO Activity_Data (est_id, source_id, period, quantity, provenance_id) VALUES (?,?,?,?,?)",
                 (est, src, str(PERIOD_END), act_t * 1000, prov))
    added += 1
conn.commit()

# ---- verification against first principles
red = conn.execute("SELECT reduction_percent FROM Reduction_Policy WHERE sector='Energy' "
                   "ORDER BY effective_year DESC LIMIT 1").fetchone()[0]
bad = 0; surplus = deficit = 0; sur_t = def_t = 0.0
for est, base, act, credit, gen in conn.execute("""
        SELECT e.est_id, b.baseline_emission_kg, er.emission_kg, cc.credit, w.generated_credit
        FROM Establishment e JOIN Baseline_Emission b ON b.est_id=e.est_id AND b.provenance_id=?
        JOIN Activity_Data ad ON ad.est_id=e.est_id AND ad.source_id=?
        JOIN Emission_Record er ON er.activity_id=ad.activity_id
        JOIN Carbon_Credit cc ON cc.est_id=e.est_id JOIN Credit_Wallet w ON w.est_id=e.est_id""", (prov, src)):
    expect = round((base * (1 - red / 100) - act) / 1000, 2)
    if abs(expect - credit) > 0.011 or abs(max(credit, 0) - gen) > 0.011:
        bad += 1
    if credit > 0: surplus += 1; sur_t += credit
    elif credit < 0: deficit += 1; def_t += credit
print(f"Added {added} stations ({skipped} skipped: no CO2 reported in both FY{FY[BASE_END-2018]} and FY{FY[PERIOD_END-2018]})")
print(f"Baseline FY{FY[BASE_END-2018]}, period FY{FY[PERIOD_END-2018]}, Energy reduction target {red}%")
print(f"Surplus stations: {surplus}  ({sur_t:,.0f} tCO2e tradable)   Deficit stations: {deficit}  ({def_t:,.0f} tCO2e)")
print("Verification (credit == (baseline x (1-target) - actual)/1000, wallet == credit):", "PASS" if bad == 0 else f"FAIL ({bad})")
conn.close()
print("DONE")
