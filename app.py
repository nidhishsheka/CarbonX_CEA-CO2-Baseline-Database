# Dear programmer:
# When I wrote this code, only god and // I knew how it worked.
# Now, only god knows it!
# Therefore, if you are trying to optimize
# this routine and it fails (most surely),
# please increase this counter as a
# warning for the next person:

# total hours wasted here = 47


#6174_hn.sheka

import sqlite3
from flask import Flask, render_template, request, redirect
from ml_model import get_price_prediction

app = Flask(__name__)

def get_db():
    conn = sqlite3.connect("cc.db")
    conn.row_factory = sqlite3.Row
    return conn


def ensure_wallet_system(conn):
    
    columns = [row["name"] for row in conn.execute("PRAGMA table_info(Credit_Wallet)").fetchall()]

    if "generated_credit" not in columns:
        conn.execute("""
            ALTER TABLE Credit_Wallet
            ADD COLUMN generated_credit REAL NOT NULL DEFAULT 0
        """)

    # prathi est.g onji credit row n wallet row thikkund
    conn.execute("""
        INSERT OR IGNORE INTO Carbon_Credit (est_id, credit, status)
        SELECT est_id, 0, 'Neutral'
        FROM Establishment
    """)
    conn.execute("""
        INSERT OR IGNORE INTO Credit_Wallet (est_id, available_credit, reserved_credit, generated_credit)
        SELECT est_id, 0, 0, 0
        FROM Establishment
    """)

    # available wallet = generated credits + net purchased/sold credits.
    conn.execute("""
        UPDATE Credit_Wallet
        SET generated_credit = COALESCE((
            SELECT MAX(COALESCE(cc.credit, 0), 0)
            FROM Carbon_Credit cc
            WHERE cc.est_id = Credit_Wallet.est_id
        ), 0)
    """)

    conn.execute("""
        UPDATE Credit_Wallet
        SET available_credit = MAX(
            reserved_credit,
            generated_credit + COALESCE((
                SELECT SUM(
                    CASE
                        WHEN cl.transaction_type = 'Credit Purchase' THEN cl.change_amount
                        WHEN cl.transaction_type = 'Credit Sale' THEN cl.change_amount
                        ELSE 0
                    END
                )
                FROM Credit_Ledger cl
                WHERE cl.est_id = Credit_Wallet.est_id
            ), 0)
        ),
        updated_at = CURRENT_TIMESTAMP
    """)

    conn.execute("DROP TRIGGER IF EXISTS log_credit_change")
    conn.execute("DROP TRIGGER IF EXISTS sync_wallet_from_credit")

    conn.execute("""
        CREATE TRIGGER sync_wallet_from_credit
        AFTER UPDATE OF credit ON Carbon_Credit
        FOR EACH ROW
        WHEN ROUND(COALESCE(NEW.credit, 0) - COALESCE(OLD.credit, 0), 10) != 0
        BEGIN
            INSERT INTO Credit_Ledger
                (est_id, change_amount, transaction_type, description)
            VALUES
                (
                    NEW.est_id,
                    ROUND(
                        MAX(COALESCE(NEW.credit, 0), 0)
                        - MAX(COALESCE(OLD.credit, 0), 0),
                        10
                    ),
                    'Emission Adjustment',
                    'Generated carbon-credit balance changed after emission recalculation'
                );

            UPDATE Credit_Wallet
            SET
                generated_credit = MAX(COALESCE(NEW.credit, 0), 0),
                available_credit = MAX(
                    reserved_credit,
                    available_credit
                    + (
                        MAX(COALESCE(NEW.credit, 0), 0)
                        - MAX(COALESCE(OLD.credit, 0), 0)
                    )
                ),
                updated_at = CURRENT_TIMESTAMP
            WHERE est_id = NEW.est_id;
        END
    """)

    conn.commit()


def initialize_database():
    conn = get_db()
    ensure_wallet_system(conn)
    conn.close()


initialize_database()


@app.route("/")
def dashboard():

    with sqlite3.connect("cc.db") as conn:
        conn.row_factory = sqlite3.Row

    total_est = conn.execute(
        "SELECT COUNT(*) FROM Establishment"
    ).fetchone()[0]

    total_act = conn.execute(
        "SELECT COUNT(*) FROM Activity_Data"
    ).fetchone()[0]

    total_em = conn.execute(
        "SELECT COUNT(*) FROM Emission_Record"
    ).fetchone()[0]

    return render_template(
        "dashboard.html",
        total_est=total_est,
        total_act=total_act,
        total_em=total_em
    )


@app.route("/add_establishment", methods=["GET","POST"])
def add_establishment():

    with sqlite3.connect("cc.db") as conn:
        conn.row_factory = sqlite3.Row
    sectors = conn.execute(
        "SELECT DISTINCT sector FROM Reduction_Policy"
        ).fetchall()

    if request.method == "POST":

        name = request.form["entity_name"]
        est_type = request.form["entity_type"]
        location = request.form["location"]

        baseline = request.form.get("baseline") or 9000
        baseline_year = request.form.get("baseline_year") or 2024

        

        cursor = conn.execute(
           "INSERT INTO Establishment (est_name, est_type, location) VALUES (?,?,?)",
           (name, est_type, location)
        )

        est_id = cursor.lastrowid

        conn.execute(
    "INSERT INTO Baseline_Emission (est_id, baseline_emission_kg, baseline_year) VALUES (?,?,?)",
    (est_id, baseline, baseline_year)
)

        conn.execute(
            "INSERT INTO Carbon_Credit (est_id, credit, status) VALUES (?, 0, 'Neutral')",
            (est_id,)
        )

        conn.execute(
            """INSERT INTO Credit_Wallet
               (est_id, available_credit, reserved_credit, generated_credit)
               VALUES (?, 0, 0, 0)""",
            (est_id,)
        )

        conn.commit()

        return redirect("/")

    return render_template("add_entity.html", sectors=sectors)


@app.route("/add_activity", methods=["GET", "POST"])
def add_activity():

    conn = get_db()

    if request.method == "POST":

        est_id = request.form["est_id"]
        source_id = request.form["source_id"]
        quantity = float(request.form["quantity"])
        period = request.form["period"]

        # Insert activity.
        # The database trigger automatically creates
        # the corresponding Emission_Record.
        conn.execute(
            """
            INSERT INTO Activity_Data
            (est_id, source_id, quantity, period)
            VALUES (?, ?, ?, ?)
            """,
            (est_id, source_id, quantity, period)
        )

        conn.commit()
        conn.close()

        return redirect("/emissions")

    # GET request
    establishments = conn.execute(
        """
        SELECT est_id, est_name
        FROM Establishment
        ORDER BY est_name
        """
    ).fetchall()

    sources = conn.execute(
        """
        SELECT source_id, source_name
        FROM Emission_Source
        ORDER BY source_name
        """
    ).fetchall()

    conn.close()

    return render_template(
        "add_activity.html",
        establishments=establishments,
        sources=sources
    )



@app.route("/emissions")
def emissions():

    with sqlite3.connect("cc.db") as conn:
        conn.row_factory = sqlite3.Row

        rows = conn.execute("""
        SELECT
          e.est_name,
          s.source_name,
          s.unit,
          ad.quantity,
          ef.factor_value,
          er.emission_kg

        FROM Emission_Record er

        JOIN Activity_Data ad
        ON er.activity_id = ad.activity_id

        JOIN Establishment e
        ON ad.est_id = e.est_id

        JOIN Emission_Source s
        ON ad.source_id = s.source_id

        JOIN Emission_Factor ef
        ON er.factor_id = ef.factor_id
        """).fetchall()

    clean_rows = []

    for r in rows:
        clean_rows.append({
            "est_name": r["est_name"],
            "source_name": r["source_name"],
            "unit": r["unit"],
            "quantity": r["quantity"],
            "factor_value": r["factor_value"],
            "emission_kg": round(r["emission_kg"], 2)
        })

    return render_template("emissions.html", rows=clean_rows)


@app.route("/report")
def report():

    with sqlite3.connect("cc.db") as conn:
        conn.row_factory = sqlite3.Row

    rows = conn.execute("SELECT * FROM Carbon_Report_View").fetchall()


    data = []

    for r in rows:

        if r["activity_count"] == 0:
            credit = 0
            actual = "No activity"
            status = "No Activity"
        else:
            credit = r["carbon_credit"] if r["carbon_credit"] is not None else 0
            actual = r["actual_emission"]

            if credit > 0:
                status = "Surplus"
            elif credit < 0:
                status = "Deficit"
            else:
                status = "Neutral"

        data.append({
          "name": r["est_name"],
          "sector": r["sector"],
          "reduction": r["reduction_percent"] or 0,
          "baseline": r["baseline_emission_kg"],
          "allowed": r["allowed_limit"],
          "actual": actual,
          "credit": round(credit,2),
          "status": status
         })


    return render_template("report.html", rows=data)

@app.route("/trade", methods=["GET", "POST"])
def trade():
    conn = get_db()

    if request.method == "POST":
        seller_est_id = int(request.form["seller_est_id"])
        buyer_est_id = int(request.form["buyer_est_id"])
        credit_amount = float(request.form["credit_amount"])
        price_per_credit = float(request.form["price_per_credit"])
        total_amount = credit_amount * price_per_credit

        if seller_est_id == buyer_est_id:
            conn.close()
            return "Seller and buyer cannot be the same establishment."

        if credit_amount <= 0 or price_per_credit < 0:
            conn.close()
            return "Invalid credit amount or price."

        try:
            conn.execute("INSERT INTO Credit_Transaction (seller_est_id,buyer_est_id,credit_amount,price_per_credit,total_amount) VALUES (?,?,?,?,?)", (seller_est_id,buyer_est_id,credit_amount,price_per_credit,total_amount))
            conn.commit()
        except sqlite3.IntegrityError as e:
            conn.rollback()
            conn.close()
            return f"Transaction failed: {e}"

        conn.close()
        return redirect("/trade")

    establishments = conn.execute(
    """
    SELECT est_id, est_name
    FROM Establishment
    ORDER BY est_name
    """
    ).fetchall()

    wallets = conn.execute(
    """
    SELECT
        cw.est_id,
        e.est_name,
        cw.available_credit,
        cw.reserved_credit
    FROM Credit_Wallet cw
    JOIN Establishment e
        ON cw.est_id = e.est_id
    ORDER BY e.est_name
    """
    ).fetchall()

    transactions = conn.execute("""
    SELECT ct.transaction_id,
           s.est_name AS seller,
           b.est_name AS buyer,
           ct.credit_amount,
           ct.price_per_credit,
           ct.total_amount,
           ct.status,
           ct.created_at
    FROM Credit_Transaction ct
    JOIN Establishment s ON ct.seller_est_id = s.est_id
    JOIN Establishment b ON ct.buyer_est_id = b.est_id
    ORDER BY ct.created_at DESC
    LIMIT 3
    """).fetchall()

    listings = conn.execute(
    """
    SELECT
        cl.listing_id,
        cl.seller_est_id,
        e.est_name,
        cl.credit_amount,
        cl.remaining_credit,
        cl.price_per_credit,
        cl.status,
        cl.created_at
    FROM Credit_Listing cl
    JOIN Establishment e
        ON cl.seller_est_id = e.est_id
    WHERE cl.status = 'Active'
      AND cl.remaining_credit > 0
    ORDER BY cl.created_at DESC
    """
    ).fetchall()

    active_listings = conn.execute(
    """
    SELECT COUNT(*)
    FROM Credit_Listing
    WHERE status = 'Active'
    """
    ).fetchone()[0]

    credits_available = conn.execute(
    """
    SELECT COALESCE(SUM(remaining_credit), 0)
    FROM Credit_Listing
    WHERE status = 'Active'
    """
    ).fetchone()[0]

    average_price = conn.execute(
    """
    SELECT COALESCE(AVG(price_per_credit), 0)
    FROM Credit_Listing
    WHERE status = 'Active'
    """
    ).fetchone()[0]

    highest_price = conn.execute(
    """
    SELECT COALESCE(MAX(price_per_credit), 0)
    FROM Credit_Listing
    WHERE status = 'Active'
    """
    ).fetchone()[0]

    lowest_price = conn.execute(
    """
    SELECT COALESCE(MIN(price_per_credit), 0)
    FROM Credit_Listing
    WHERE status = 'Active'
    """
    ).fetchone()[0]

    total_trade_value = conn.execute(
    """
    SELECT COALESCE(SUM(total_amount), 0)
    FROM Credit_Transaction
    WHERE status = 'Completed'
    """
    ).fetchone()[0]

    prediction = get_price_prediction()

    price_history = conn.execute(
    """
    SELECT
        created_at,
        price_per_credit,
        credit_amount,
        total_amount
    FROM Credit_Transaction
    WHERE status = 'Completed'
    ORDER BY created_at ASC
    """
    ).fetchall()
    price_dates = [row["created_at"] for row in price_history]
    price_values = [row["price_per_credit"] for row in price_history]

    daily_market = conn.execute(
    """
    SELECT
        DATE(created_at) AS trade_date,
        ROUND(AVG(price_per_credit), 2) AS avg_price,
        SUM(credit_amount) AS credits_traded,
        ROUND(SUM(total_amount), 2) AS trade_value
    FROM Credit_Transaction
    WHERE status = 'Completed'
    GROUP BY DATE(created_at)
    ORDER BY trade_date ASC
    """
    ).fetchall()

    market_dates = [row["trade_date"] for row in daily_market]
    market_prices = [row["avg_price"] for row in daily_market]

    moving_average = []

    for i in range(len(market_prices)):
        start = max(0, i - 2)
        window = market_prices[start:i + 1]
        moving_average.append(round(sum(window) / len(window), 2))

    current_price = market_prices[-1] if market_prices else 0
    previous_price = market_prices[-2] if len(market_prices) > 1 else current_price

    if previous_price:
        price_change = ((current_price - previous_price) / previous_price) * 100
    else:
        price_change = 0

    if price_change > 1:
        market_trend = "UP"
    elif price_change < -1:
        market_trend = "DOWN"
    else:
        market_trend = "STABLE"

    conn.close()

    return render_template("trade.html", establishments=establishments, wallets=wallets, listings=listings, active_listings=active_listings, credits_available=credits_available, average_price=average_price, highest_price=highest_price, lowest_price=lowest_price, total_trade_value=total_trade_value, price_history=price_history, price_dates=price_dates, price_values=price_values, market_dates=market_dates, market_prices=market_prices, current_price=current_price, price_change=price_change, market_trend=market_trend, moving_average=moving_average, prediction=prediction, transactions=transactions)

@app.route("/market")
def market():
    conn = get_db()

    market_trend = get_market_trend(conn)

    market_data = conn.execute("""
        SELECT *
        FROM Market_History
        ORDER BY trade_date DESC
    """).fetchall()

    conn.close()

    prediction = get_price_prediction()

    return render_template(
        "market.html",
        market_data=market_data,
        prediction=prediction, market_trend=market_trend
    )

def update_market_history(conn):
    market = conn.execute("""
        SELECT
            DATE(created_at) AS trade_date,
            AVG(price_per_credit) AS avg_price,
            SUM(credit_amount) AS credits_traded,
            SUM(total_amount) AS trade_value
        FROM Credit_Transaction
        WHERE status = 'Completed'
          AND DATE(created_at) = DATE('now','localtime')
    """).fetchone()

    if market["avg_price"] is None:
        return

    active_listings = conn.execute("""
        SELECT COUNT(*) AS count
        FROM Credit_Listing
        WHERE status = 'Active'
    """).fetchone()["count"]

    credits_available = conn.execute("""
        SELECT COALESCE(SUM(remaining_credit), 0) AS credits
        FROM Credit_Listing
        WHERE status = 'Active'
    """).fetchone()["credits"]

    conn.execute("""
        INSERT INTO Market_History
        (trade_date, avg_price, credits_traded, trade_value,
         active_listings, credits_available)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(trade_date)
        DO UPDATE SET
            avg_price = excluded.avg_price,
            credits_traded = excluded.credits_traded,
            trade_value = excluded.trade_value,
            active_listings = excluded.active_listings,
            credits_available = excluded.credits_available
    """, (
        market["trade_date"],
        market["avg_price"],
        market["credits_traded"],
        market["trade_value"],
        active_listings,
        credits_available
    ))

def get_market_trend(conn):
    rows = conn.execute("""
        SELECT trade_date, avg_price, credits_traded, trade_value
        FROM Market_History
        ORDER BY trade_date ASC
    """).fetchall()

    if len(rows) < 2:
        return {
            "direction": "STABLE",
            "price_change": 0,
            "description": "Not enough historical data to determine market direction."
        }

    first_price = rows[0]["avg_price"]
    latest_price = rows[-1]["avg_price"]

    if first_price == 0:
        price_change = 0
    else:
        price_change = ((latest_price - first_price) / first_price) * 100

    if price_change > 5:
        direction = "BULLISH"
        description = "Carbon credit prices are trending upward."
    elif price_change < -5:
        direction = "BEARISH"
        description = "Carbon credit prices are trending downward."
    else:
        direction = "STABLE"
        description = "Carbon credit prices are relatively stable."

    total_volume = sum(row["credits_traded"] for row in rows)

    return {
        "direction": direction,
        "price_change": round(price_change, 2),
        "description": description,
        "total_volume": total_volume
    }

@app.route("/buy-credit", methods=["POST"])
def buy_credit():
    conn = get_db()

    try:
        listing_id = int(request.form["listing_id"])
        buyer_est_id = int(request.form["buyer_est_id"])
        quantity = float(request.form["quantity"])

        listing = conn.execute("SELECT listing_id,seller_est_id,remaining_credit,price_per_credit,status FROM Credit_Listing WHERE listing_id=?", (listing_id,)).fetchone()

        if listing is None:
            return "Listing not found", 404

        if listing["status"] != "Active":
            return "This listing is no longer active", 400

        if quantity <= 0:
            return "Invalid quantity", 400

        if quantity > listing["remaining_credit"]:
            return "Not enough credits available in this listing", 400

        if buyer_est_id == listing["seller_est_id"]:
            return "Seller cannot buy their own listing", 400

        total_amount = quantity * listing["price_per_credit"]

        conn.execute(
            """
            INSERT INTO Credit_Transaction (
                seller_est_id,
                buyer_est_id,
                credit_amount,
                price_per_credit,
                total_amount
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                listing["seller_est_id"],
                buyer_est_id,
                quantity,
                listing["price_per_credit"],
                total_amount
            )
        )

        conn.execute(
            """
            UPDATE Credit_Listing
            SET
                remaining_credit = remaining_credit - ?,
                status = CASE
                    WHEN remaining_credit - ? = 0
                        THEN 'Sold'
                    ELSE 'Active'
                END
            WHERE listing_id = ?
                AND status = 'Active'
                AND remaining_credit >= ?
            """,
            (
                quantity,
                quantity,
                listing_id,
                quantity
            )
        )

        conn.execute(
            """
            UPDATE Credit_Wallet
            SET
                reserved_credit = reserved_credit - ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE est_id = ?
            """,
            (
                quantity,
                listing["seller_est_id"]
            )
        )

        update_market_history(conn)
        conn.commit()

        return redirect("/trade")

    except Exception as e:
        conn.rollback()
        return str(e), 400

    finally:
        conn.close()

@app.route("/create-listing", methods=["POST"])
def create_listing():
    conn = get_db()

    try:
        seller_est_id = int(request.form["seller_est_id"])
        credit_amount = float(request.form["credit_amount"])
        price_per_credit = float(request.form["price_per_credit"])

        if credit_amount <= 0 or price_per_credit <= 0:
            return "Invalid listing values", 400

        wallet = conn.execute(
            """
            SELECT
                available_credit,
                reserved_credit
            FROM Credit_Wallet
            WHERE est_id = ?
            """,
            (seller_est_id,)
        ).fetchone()

        if wallet is None:
            return "Seller wallet not found", 404

        unreserved_credit = wallet["available_credit"] - wallet["reserved_credit"]

        if unreserved_credit < credit_amount:
            return "Insufficient unreserved credits", 400

        conn.execute(
            """
            INSERT INTO Credit_Listing (
                seller_est_id,
                credit_amount,
                remaining_credit,
                price_per_credit
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                seller_est_id,
                credit_amount,
                credit_amount,
                price_per_credit
            )
        )

        conn.execute(
            """
            UPDATE Credit_Wallet
            SET
                reserved_credit = reserved_credit + ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE est_id = ?
            """,
            (
                credit_amount,
                seller_est_id
            )
        )

        conn.commit()

        return redirect("/trade")

    except Exception as e:
        conn.rollback()
        return str(e), 400

    finally:
        conn.close()

@app.route("/transactions")
def transactions():
    conn = get_db()
    transactions = conn.execute("""
        SELECT ct.transaction_id,
               s.est_name AS seller,
               b.est_name AS buyer,
               ct.credit_amount,
               ct.price_per_credit,
               ct.total_amount,
               ct.status,
               ct.created_at
        FROM Credit_Transaction ct
        JOIN Establishment s ON ct.seller_est_id = s.est_id
        JOIN Establishment b ON ct.buyer_est_id = b.est_id
        ORDER BY ct.created_at DESC
    """).fetchall()
    conn.close()
    return render_template("transactions.html", transactions=transactions)

if __name__ == "__main__":
    app.run(debug=True)