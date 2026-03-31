from pyspark.sql import functions as F
from pyspark.sql import Window

# Parameters
start_date = 20260102
interval_days = 7
lookback_days = 14
num_windows = 10  # how many 7-day checkpoints you want

# Build list of evaluation dates
from datetime import datetime, timedelta
start_dt = datetime.strptime(str(start_date), "%Y%m%d")
eval_dates = [
    int((start_dt + timedelta(days=i * interval_days)).strftime("%Y%m%d"))
    for i in range(num_windows)
]

# For each eval date, compute the lookback start as a datekey int
# We'll explode eval dates and cross-join logic via a helper
eval_df = spark.createDataFrame(
    [(d, int((datetime.strptime(str(d), "%Y%m%d") - timedelta(days=lookback_days)).strftime("%Y%m%d")))
     for d in eval_dates],
    ["eval_datekey", "lb_start_datekey"]
)

# Join: each transaction falls into every eval window where
# lb_start_datekey < datekey <= eval_datekey
joined = (
    df.join(F.broadcast(eval_df),
            (df.datekey > eval_df.lb_start_datekey) &
            (df.datekey <= eval_df.eval_datekey))
)

# Aggregate per eval_datekey, acct_id, cdi_cd
agg = (
    joined
    .groupBy("eval_datekey", "acct_id", "cdi_cd")
    .agg(
        F.sum("amount").alias("total_amount"),
        F.count("*").alias("trxn_cnt")
    )
)

# Filter by thresholds (adjust D thresholds as needed)
credit_hits = (
    agg.filter(
        (F.col("cdi_cd") == "C") &
        (F.col("total_amount").between(10000, 100000000)) &
        (F.col("trxn_cnt").between(4, 10000))
    )
)

debit_hits = (
    agg.filter(
        (F.col("cdi_cd") == "D") &
        (F.col("total_amount").between(10000, 100000000)) &  # swap in your D thresholds
        (F.col("trxn_cnt").between(4, 10000))
    )
)

# Count distinct accounts per eval window
credit_summary = (
    credit_hits
    .groupBy("eval_datekey")
    .agg(F.countDistinct("acct_id").alias("credit_acct_count"))
)

debit_summary = (
    debit_hits
    .groupBy("eval_datekey")
    .agg(F.countDistinct("acct_id").alias("debit_acct_count"))
)

# Combine
summary = (
    credit_summary
    .join(debit_summary, "eval_datekey", "outer")
    .orderBy("eval_datekey")
    .fillna(0)
)

summary.show(truncate=False)
