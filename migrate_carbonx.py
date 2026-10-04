import shutil
import sqlite3
import sys
from datetime import datetime

db_path = sys.argv[1] if len(sys.argv) > 1 else "cc.db"
backup = f"{db_path}.bak_{datetime.now():%Y%m%d_%H%M%S}"
shutil.copy2(db_path, backup)
print(f"Backup written: {backup}")

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row


def has_col(table, col):
    return col in [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]


# ---------------------------------------------------------------- 1 + 2
removed = conn.execute("""
    DELETE FROM Reduction_Policy
    WHERE policy_id NOT IN (
        SELECT MIN(policy_id) FROM Reduction_Policy GROUP BY sector, effective_year)
""").rowcount
print(f"Reduction_Policy: removed {removed} duplicate rows")
conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_policy_sector_year "
             "ON Reduction_Policy(sector, effective_year)")
conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_baseline_est_year "
             "ON Baseline_Emission(est_id, baseline_year)")

# ---------------------------------------------------------------- 3
conn.execute("""
CREATE TABLE IF NOT EXISTS Data_Provenance (
    provenance_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_name    TEXT NOT NULL,
    organization    TEXT,
    dataset_version TEXT,
    source_url      TEXT,
    license         TEXT,
    retrieval_date  TEXT,
    methodology     TEXT,
    is_synthetic    INTEGER NOT NULL DEFAULT 0
)""")
legacy = conn.execute(
    "SELECT provenance_id FROM Data_Provenance WHERE dataset_name = 'CarbonX seed data (unsourced)'"
).fetchone()
if legacy is None:
    legacy_id = conn.execute("""
        INSERT INTO Data_Provenance
            (dataset_name, organization, methodology, is_synthetic)
        VALUES ('CarbonX seed data (unsourced)', 'CarbonX developers',
                'Hand-entered demo values; no external source', 1)
    """).lastrowid
else:
    legacy_id = legacy["provenance_id"]

for table in ("Emission_Factor", "Baseline_Emission", "Activity_Data"):
    if not has_col(table, "provenance_id"):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN provenance_id INTEGER "
                     f"REFERENCES Data_Provenance(provenance_id)")
    conn.execute(f"UPDATE {table} SET provenance_id = ? WHERE provenance_id IS NULL",
                 (legacy_id,))
print(f"Provenance: existing rows labelled as synthetic seed data (id {legacy_id})")

# ---------------------------------------------------------------- 4
conn.executescript("""
DROP TRIGGER IF EXISTS update_carbon_credit;
DROP VIEW IF EXISTS Carbon_Report_View;
DROP VIEW IF EXISTS Credit_By_Period;
DROP VIEW IF EXISTS Establishment_Limit;
DROP VIEW IF EXISTS Establishment_Period_Emission;

-- emissions per establishment per period (no joins that can multiply rows)
CREATE VIEW Establishment_Period_Emission AS
SELECT ad.est_id, ad.period,
       COUNT(*)           AS activity_count,
       SUM(er.emission_kg) AS actual_kg
FROM Activity_Data ad
JOIN Emission_Record er ON er.activity_id = ad.activity_id
GROUP BY ad.est_id, ad.period;

-- one limit per establishment: latest baseline, latest sector policy
CREATE VIEW Establishment_Limit AS
SELECT e.est_id,
       COALESCE(b.baseline_emission_kg, 0)                    AS baseline_kg,
       COALESCE(rp.reduction_percent, 0)                      AS reduction_percent,
       COALESCE(b.baseline_emission_kg, 0)
         * (1 - COALESCE(rp.reduction_percent, 0) / 100.0)    AS allowed_kg
FROM Establishment e
LEFT JOIN Baseline_Emission b ON b.baseline_id = (
    SELECT baseline_id FROM Baseline_Emission
    WHERE est_id = e.est_id
    ORDER BY baseline_year DESC, baseline_id DESC LIMIT 1)
LEFT JOIN Reduction_Policy rp ON rp.policy_id = (
    SELECT policy_id FROM Reduction_Policy
    WHERE sector = e.est_type
    ORDER BY effective_year DESC, policy_id DESC LIMIT 1);

CREATE VIEW Credit_By_Period AS
SELECT p.est_id, p.period, l.allowed_kg, p.actual_kg,
       (l.allowed_kg - p.actual_kg)          AS credit_kg,
       ROUND((l.allowed_kg - p.actual_kg) / 1000.0, 2) AS credit_t
FROM Establishment_Period_Emission p
JOIN Establishment_Limit l ON l.est_id = p.est_id;

CREATE VIEW Carbon_Report_View AS
SELECT e.est_id, e.est_name, e.est_type AS sector,
       l.baseline_kg                                   AS baseline_emission_kg,
       l.reduction_percent                             AS reduction_percent,
       COALESCE(a.activity_count, 0)                   AS activity_count,
       ROUND(COALESCE(a.actual_kg, 0), 2)              AS actual_emission,
       ROUND(l.allowed_kg * MAX(COALESCE(a.periods, 0), 1), 2) AS allowed_limit,
       CASE WHEN COALESCE(a.activity_count, 0) = 0 THEN 0
            ELSE ROUND((l.allowed_kg * a.periods - a.actual_kg) / 1000.0, 2)
       END                                             AS carbon_credit
FROM Establishment e
JOIN Establishment_Limit l ON l.est_id = e.est_id
LEFT JOIN (SELECT est_id,
                  COUNT(*)            AS periods,
                  SUM(activity_count) AS activity_count,
                  SUM(actual_kg)      AS actual_kg
           FROM Establishment_Period_Emission GROUP BY est_id) a
       ON a.est_id = e.est_id;

CREATE TRIGGER update_carbon_credit AFTER INSERT ON Emission_Record
FOR EACH ROW
BEGIN
    UPDATE Carbon_Credit
    SET credit = ROUND(COALESCE((
            SELECT SUM(credit_kg) FROM Credit_By_Period
            WHERE est_id = (SELECT est_id FROM Activity_Data
                            WHERE activity_id = NEW.activity_id)), 0) / 1000.0, 2)
    WHERE est_id = (SELECT est_id FROM Activity_Data WHERE activity_id = NEW.activity_id);

    UPDATE Carbon_Credit
    SET status = CASE WHEN credit > 0 THEN 'Surplus'
                      WHEN credit < 0 THEN 'Deficit'
                      ELSE 'Neutral' END
    WHERE est_id = (SELECT est_id FROM Activity_Data WHERE activity_id = NEW.activity_id);
END;
""")
print("Credit calculation: period-aware views and trigger installed")

# ---------------------------------------------------------------- 5
before = {r["est_id"]: r["credit"] for r in conn.execute("SELECT est_id, credit FROM Carbon_Credit")}
conn.execute("""
UPDATE Carbon_Credit
SET credit = ROUND(COALESCE((SELECT SUM(credit_kg) FROM Credit_By_Period p
                             WHERE p.est_id = Carbon_Credit.est_id), 0) / 1000.0, 2)
""")
conn.execute("""
UPDATE Carbon_Credit
SET status = CASE WHEN credit > 0 THEN 'Surplus'
                  WHEN credit < 0 THEN 'Deficit' ELSE 'Neutral' END
""")
conn.commit()

print("\nCredit corrections (stored credit, tCO2e):")
changed = 0
for r in conn.execute("""SELECT cc.est_id, e.est_name, cc.credit
                         FROM Carbon_Credit cc JOIN Establishment e USING(est_id)"""):
    old, new = before[r["est_id"]], r["credit"]
    if abs((old or 0) - (new or 0)) > 1e-9:
        changed += 1
        print(f"  {r['est_name']:<28} {old:>8} -> {new:>8}")
print(f"  {changed} establishment(s) corrected" if changed else "  none")

# ---------------------------------------------------------------- 6
print("\nVerification")
ok = True

bad = conn.execute("""
    SELECT e.est_name, cc.credit AS stored, v.carbon_credit AS reported
    FROM Carbon_Credit cc
    JOIN Carbon_Report_View v ON v.est_id = cc.est_id
    JOIN Establishment e ON e.est_id = cc.est_id
    WHERE ABS(cc.credit - v.carbon_credit) > 0.011
""").fetchall()
print(f"  stored credit == report view: {'PASS' if not bad else 'FAIL'}")
for r in bad:
    ok = False
    print(f"    {r['est_name']}: stored {r['stored']} vs report {r['reported']}")

bad = conn.execute("""
    SELECT e.est_name,
           ROUND(SUM(ad.quantity * ef.factor_value), 4) AS expected,
           ROUND((SELECT SUM(emission_kg) FROM Emission_Record er
                  JOIN Activity_Data a2 ON a2.activity_id = er.activity_id
                  WHERE a2.est_id = e.est_id), 4)         AS recorded
    FROM Establishment e
    JOIN Activity_Data ad ON ad.est_id = e.est_id
    JOIN Emission_Factor ef ON ef.source_id = ad.source_id AND ef.is_active = 1
    GROUP BY e.est_id
    HAVING ABS(expected - COALESCE(recorded, 0)) > 0.01
""").fetchall()
print(f"  emissions == quantity x active factor: {'PASS' if not bad else 'FAIL'}")
for r in bad:
    ok = False
    print(f"    {r['est_name']}: expected {r['expected']} vs recorded {r['recorded']}")

orphans = conn.execute("""
    SELECT e.est_name, e.est_type FROM Establishment e
    WHERE NOT EXISTS (SELECT 1 FROM Reduction_Policy rp WHERE rp.sector = e.est_type)
""").fetchall()
if orphans:
    print("  WARNING - sector has no Reduction_Policy row (treated as 0% reduction):")
    for r in orphans:
        print(f"    {r['est_name']} ({r['est_type']})")

conn.close()
print("\nDONE" if ok else "\nDONE WITH FAILURES - restore from backup and investigate")