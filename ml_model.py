import sqlite3
import pandas as pd
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error, r2_score

DB_NAME = "cc.db"


def get_price_prediction():
    conn = sqlite3.connect(DB_NAME)

    query = "SELECT trade_date, avg_price, credits_traded, trade_value, active_listings, credits_available FROM Market_History ORDER BY trade_date ASC"

    df = pd.read_sql_query(query, conn)

    conn.close()

    if len(df) < 5:
        return {
            "available": False,
            "message": f"Need more historical data. Currently have {len(df)} trading periods."
        }

    df["previous_price"] = df["avg_price"].shift(1)
    df["moving_average"] = df["avg_price"].rolling(3).mean()
    df["price_change"] = df["avg_price"].pct_change() * 100
    df["target_price"] = df["avg_price"].shift(-1)

    df = df.dropna()

    if len(df) < 5:
        return {
            "available": False,
            "message": "Not enough usable data after feature engineering."
        }

    features = [
        "previous_price",
        "moving_average",
        "price_change",
        "credits_traded",
        "trade_value",
        "active_listings",
        "credits_available"
    ]

    X = df[features]
    y = df["target_price"]

    split = int(len(df) * 0.8)

    if split == 0 or split >= len(df):
        return {
            "available": False,
            "message": "Not enough data for model evaluation."
        }

    X_train = X.iloc[:split]
    X_test = X.iloc[split:]

    y_train = y.iloc[:split]
    y_test = y.iloc[split:]

    model = LinearRegression()

    model.fit(X_train, y_train)

    predictions = model.predict(X_test)

    mae = mean_absolute_error(y_test, predictions)

    latest = df.iloc[-1]

    latest_features = pd.DataFrame([{
        "previous_price": latest["avg_price"],
        "moving_average": latest["moving_average"],
        "price_change": latest["price_change"],
        "credits_traded": latest["credits_traded"],
        "trade_value": latest["trade_value"],
        "active_listings": latest["active_listings"],
        "credits_available": latest["credits_available"]
    }])

    predicted_price = model.predict(latest_features)[0]

    return {
        "available": True,
        "predicted_price": round(predicted_price, 2),
        "current_price": round(latest["avg_price"], 2),
        "mae": round(mae, 2)
    }


if __name__ == "__main__":
    result = get_price_prediction()

    print("\nAI Forecast:")
    print(result)