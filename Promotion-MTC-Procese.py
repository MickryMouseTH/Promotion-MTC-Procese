from LogLibrary import Load_Config, Loguru_Logging
import pyodbc
import time
import json
import os
import sys
import csv

# ----------------------- Configuration Values -----------------------
Program_Name = "Promotion-MTC-Procese"
Program_Version = "1.3.4"
# ---------------------------------------------------------------------

# Default configuration for database connections and logging
default_config = {
    "Toll_DB":{
        "DB_SERVER": "",
        "DB_DATABASE": "",
        "DB_USERNAME": "",
        "DB_PASSWORD": "",
        "ODBC_Driver": "ODBC Driver 17 for SQL Server",
        "QUERY_LIMIT": 100,
        "Back_date": 7,
        "Back_Time": 10,
        "Start_Date": "2025-08-07 00:00:00.000"
    },
    "Promotion_DB":{
        "DB_SERVER": "",
        "DB_DATABASE": "",
        "DB_USERNAME": "",
        "DB_PASSWORD": "",
        "ODBC_Driver": "ODBC Driver 17 for SQL Server",
    },
    "RETRY_INTERVAL": 10,  # seconds
    "Deadlock_Max_Retry": 3,          # How many times to retry a single row that hits a SQL Server deadlock (1205).
    "Deadlock_Retry_Delay": 1,        # Seconds to wait between deadlock retries.
    "Dry_Run": 0,                     # Set to 1 to run a single pass and export CSV instead of writing to the DB.
    "Dry_Run_Dir": "dry_run_output",  # Output folder for dry-run CSV files.
    "log_Level": "DEBUG",
    "Log_Console": 1,         # Set to "true" to enable console logging.
    "log_Backup": 90,         # Log retention duration (number of backup files).
    "Log_Size": "10 MB"       # Maximum log file size before rotation. 
}

config = Load_Config(default_config, Program_Name)
logger = Loguru_Logging(config, Program_Name, Program_Version)

# How aggressively to retry a single row that hits a SQL Server deadlock (1205).
try:
    DEADLOCK_MAX_RETRY = int(config.get('Deadlock_Max_Retry', 3))
except (TypeError, ValueError):
    DEADLOCK_MAX_RETRY = 3
try:
    DEADLOCK_RETRY_DELAY = float(config.get('Deadlock_Retry_Delay', 1))
except (TypeError, ValueError):
    DEADLOCK_RETRY_DELAY = 1.0

def redact_config(value):
    """
    Return a copy of the configuration with any sensitive values (e.g. database
    passwords) masked, so it is safe to write to the logs. Handles nested dicts.
    """
    if isinstance(value, dict):
        redacted = {}
        for key, val in value.items():
            if isinstance(key, str) and ("PASSWORD" in key.upper() or "PWD" in key.upper()):
                redacted[key] = "******" if val else val
            else:
                redacted[key] = redact_config(val)
        return redacted
    return value

def is_duplicate_key_error(exc):
    """
    Return True only for a SQL Server duplicate-key violation: SQLSTATE 23000
    (integrity constraint violation) together with native error 2627 (PRIMARY
    KEY / UNIQUE constraint) or 2601 (unique index). Such an error means the row
    already exists in the target table, which we treat as a successful "already
    processed" case rather than a failure.

    Other errors are intentionally NOT matched. In particular a deadlock
    (native error 1205, SQLSTATE 40001) must be treated as a real failure so the
    row is retried and NOT marked as processed in the Toll DB. The native code
    is matched inside parentheses -- "(2627)" -- so an unrelated number in the
    message (e.g. a process id in a deadlock message) cannot cause a false hit.
    """
    args = getattr(exc, "args", None) or ()
    sqlstate = args[0] if args else None
    message = str(exc)
    return sqlstate == "23000" and ("(2627)" in message or "(2601)" in message)

def is_deadlock_error(exc):
    """
    Return True if the exception is a SQL Server deadlock victim error
    (native error 1205, SQLSTATE 40001). A deadlock is transient: the
    transaction was rolled back and can simply be retried.
    """
    args = getattr(exc, "args", None) or ()
    sqlstate = args[0] if args else None
    message = str(exc)
    return sqlstate == "40001" or "(1205)" in message

def apply_deadlock_prevention(cursor):
    """
    Reduce the chance (and impact) of deadlocks for this batch job:

    - SET DEADLOCK_PRIORITY LOW: if a deadlock does occur, SQL Server picks
      THIS session as the victim, so the live toll/OLTP system is never rolled
      back on our account. We just retry (see is_deadlock_error handling).

    Best-effort only: if the session setting cannot be applied we log and carry
    on, since the retry logic still protects correctness.
    """
    try:
        cursor.execute("SET DEADLOCK_PRIORITY LOW;")
    except Exception as e:
        logger.warning(f"Could not set DEADLOCK_PRIORITY LOW: {e}")

def get_Tolldb_connection(config):
    """
    Create and return a database connection object for the Toll database.
    """
    logger.debug(f"Using configuration: {redact_config(config)}")
    try:
        conn_str = (
            f"DRIVER={config.get('ODBC_Driver', 'ODBC Driver 17 for SQL Server')};"
            f"SERVER={config.get('DB_SERVER')};"
            f"DATABASE={config.get('DB_DATABASE')};"
            f"UID={config.get('DB_USERNAME')};"
            f"PWD={config.get('DB_PASSWORD')};"
            "TrustServerCertificate=yes;"
        )
        # Avoid logging the full connection string: it contains the DB password.
        logger.debug("Connecting to Toll database "
                     f"{config.get('DB_SERVER')}/{config.get('DB_DATABASE')}")
        conn = pyodbc.connect(conn_str)
        logger.info("Database connection established successfully!")
        return conn
    except Exception as e:
        logger.error(f"Failed to connect to database: {e}")
        return None

def get_Promotiondb_connection(config):
    """
    Create and return a database connection object for the Promotion database.
    """
    logger.debug(f"Using configuration: {redact_config(config)}")
    try:
        conn_str = (
            f"DRIVER={config.get('ODBC_Driver', 'ODBC Driver 17 for SQL Server')};"
            f"SERVER={config.get('DB_SERVER')};"
            f"DATABASE={config.get('DB_DATABASE')};"
            f"UID={config.get('DB_USERNAME')};"
            f"PWD={config.get('DB_PASSWORD')};"
            "TrustServerCertificate=yes;"
        )
        # Avoid logging the full connection string: it contains the DB password.
        logger.debug("Connecting to Promotion database "
                     f"{config.get('DB_SERVER')}/{config.get('DB_DATABASE')}")
        conn = pyodbc.connect(conn_str)
        logger.info("Promotion database connection established successfully!")
        return conn
    except Exception as e:
        logger.error(f"Failed to connect to Promotion database: {e}")
        return None
       
def get_Toll_Transactions(config):
    """
    Fetch transactions from the Toll database.

    Returns a list of transaction rows on success (which may be empty when
    there genuinely are no rows). Returns None on ANY error (no connection or
    query failure) so the caller can tell "nothing to do" apart from "could not
    read" and abort instead of marking data as processed.
    """
    logger.debug(f"Using configuration: {redact_config(config)}")
    try:
        logger.info("Fetching transactions from the Toll database...")
        conn = get_Tolldb_connection(config)
        if not conn:
            logger.error("No connection to Toll database. Aborting fetch.")
            return None

        cursor = conn.cursor()
        SQL_Select = f"""
            SELECT TOP {config.get('QUERY_LIMIT', 100)}
                DMTPX_ID,
                CONCAT(
                    CONVERT(VARCHAR(8), dpt.DMTPX_TRX_DATETIME, 112),
                    RIGHT('0' + CAST(DATEPART(HOUR, dpt.DMTPX_TRX_DATETIME) AS VARCHAR), 2),
                    RIGHT('0' + CAST(DATEPART(MINUTE, dpt.DMTPX_TRX_DATETIME) AS VARCHAR), 2),
                    RIGHT('0' + CAST(DATEPART(SECOND, dpt.DMTPX_TRX_DATETIME) AS VARCHAR), 2),
                    RIGHT('00' + CAST(dpt.DMTPX_PLAZA_ID AS VARCHAR), 2),
                    RIGHT('00' + CAST(dpt.DMTPX_LANE_ID AS VARCHAR), 2),
                    RIGHT('00' + CAST(dpt.DMTPX_TC_PAYMENTMETHOD_ID AS VARCHAR), 2),
                    dpt.DMTPX_RECEIPT_NO 
                ) as ID, 
                dpt.DMTPX_TRX_DATETIME,
                dpt.DMTPX_TSB_ID,
                dpt.DMTPX_PLAZA_ID,
                dpt.DMTPX_LANE_ID,
                dpt.DMTPX_PRICE_IN_CURRENCY,
                dpt.DMTPX_LICENCEPLATE,
                dp.DMTPV_LOCAL_DESCRIPTION as DMTPX_PROVINCE,
                dpt.DMTPX_RECEIPT_NO,
                dpt.DMTPX_TC_PAYMENTMETHOD_ID 
            FROM DMT_PASSING_TRANSACTION dpt
            LEFT JOIN DMT_PROVINCE dp ON dpt.DMTPX_PROVINCEID = dp.DMTPV_PROVINCE_ID
            WHERE dpt.DMTPX_TRX_DATETIME 
                BETWEEN DATEADD(DAY,-{config.get('Back_date',7)},CONCAT(CONVERT(DATE,GETDATE()),' 00:00:00.000')) 
                    AND DATEADD(minute,-{config.get('Back_Time',10)},GETDATE())
            AND dpt.DMTPX_TRX_DATETIME >= '{config.get('Start_Date','2025-08-01 00:00:00.000')}'
            AND dpt.DMTPX_TC_PAYMENTMETHOD_ID IN (1,2,3,4,17,18,19,20)
            AND (dpt.DMTPX_LICENCEPLATE IS NOT NULL AND dpt.DMTPX_LICENCEPLATE <> '')
            AND (dpt.DMTPX_PROVINCEID IS NOT NULL AND dpt.DMTPX_PROVINCEID <> '')
            AND dpt.DMTPX_PROMOTION_DATE is null 
            ORDER BY dpt.DMTPX_TRX_DATETIME
        """
        cursor.execute(SQL_Select)
        transactions = cursor.fetchall()
        logger.info(f"Fetched {len(transactions)} transactions from the Toll database.")
        cursor.close()
        conn.close()
        return transactions

    except Exception as e:
        logger.error(f"Error fetching transactions: {e}")
        return None

def get_Promotion_Register(config):
    """
    Fetch promotion register from the Promotion database.

    Returns a list of promotion rows on success (which may be empty when there
    genuinely are no active promotions). Returns None on ANY error (no
    connection or query failure), so the caller MUST NOT proceed to mark
    transactions as processed -- otherwise transactions would be flagged as
    "checked" without ever being compared against the register.
    """
    logger.debug(f"Using configuration: {redact_config(config)}")
    try:
        logger.info("Fetching promotion register from the Promotion database...")
        conn = get_Promotiondb_connection(config)
        if not conn:
            logger.error("No connection to Promotion database. Aborting fetch.")
            return None

        cursor = conn.cursor()
        SQL_Select = """
            SELECT pmr.LICENCE_PLATE
            ,pmr.PROVINCE
            from PROMOTION_MTC_REGISTER pmr 
            WHERE pmr.LICENCE_PLATE_STATUS = 'A'
        """
        cursor.execute(SQL_Select)
        promotions = cursor.fetchall()
        logger.info(f"Fetched {len(promotions)} promotions from the Promotion database.")
        cursor.close()
        conn.close()
        return promotions

    except Exception as e:
        logger.error(f"Error fetching promotions: {e}")
        return None
    
def insert_Toll_Transactions(config, transactions):
    """
    Insert transactions into the Promotion Toll database, one record at a time.
    Each row is checked (by TRANS_NO) and committed individually, so a failure
    on one row does not roll back rows that were already inserted successfully.

    Returns the list of transactions that were successfully inserted or already
    present in the Promotion Toll database. These are the only ones that are
    safe to mark as processed in the Toll DB; rows that failed to insert are
    left out so they can be retried on the next run.
    """
    logger.debug(f"Using configuration: {redact_config(config)}")

    SQL_Insert = """
    INSERT INTO PROMOTION_TOLL
          (TRANS_NO, TRX_DATETIME, TSB_ID, PLAZA_ID, LANE_ID, PRICE, LICENCE_PLATE, PROVINCE, RECEIPT_NO, PAYMENTMETHOD_ID, STATUS, CREATE_BY, CREATE_DATETIME )
    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'W', '7710599', getdate());
    """
    SQL_Check = "SELECT COUNT(1) FROM PROMOTION_TOLL WHERE TRANS_NO = ?"

    processed = []
    conn = None
    try:
        conn = get_Promotiondb_connection(config)
        if not conn:
            logger.error("No connection to Promotion database. Insert aborted.")
            return processed

        cursor = conn.cursor()
        # Deadlock prevention: mark this batch job as the preferred deadlock
        # victim so the live toll system always wins; we simply retry.
        apply_deadlock_prevention(cursor)
        total = len(transactions)
        inserted_count = 0   # newly inserted rows
        existing_count = 0   # already present (found by the existence check)
        dup_count = 0        # duplicate-key on insert (already present, race/prior run)
        failed_count = 0     # real failures (will be retried next run)
        deadlock_retry_total = 0  # total deadlock retries across all rows
        logger.info(f"Starting insert into Promotion Toll database: {total} matched transaction(s) to process.")

        for index, transaction in enumerate(transactions, start=1):
            trans_id = getattr(transaction, 'ID', '?')
            plate = getattr(transaction, 'DMTPX_LICENCEPLATE', '?')
            province = getattr(transaction, 'DMTPX_PROVINCE', '?')
            attempt = 0
            while True:
                attempt += 1
                try:
                    cursor.execute(SQL_Check, transaction.ID)
                    exists = cursor.fetchone()[0]
                    if exists:
                        existing_count += 1
                        processed.append(transaction)
                        logger.info(f"[{index}/{total}] Transaction {trans_id} (plate={plate}, province={province}) already exists. Skipping insert.")
                        break
                    cursor.execute(SQL_Insert, transaction.ID, transaction.DMTPX_TRX_DATETIME, transaction.DMTPX_TSB_ID, transaction.DMTPX_PLAZA_ID, transaction.DMTPX_LANE_ID, transaction.DMTPX_PRICE_IN_CURRENCY, transaction.DMTPX_LICENCEPLATE, transaction.DMTPX_PROVINCE, transaction.DMTPX_RECEIPT_NO, transaction.DMTPX_TC_PAYMENTMETHOD_ID)
                    # Commit this single row so a later failure cannot undo it.
                    conn.commit()
                    inserted_count += 1
                    processed.append(transaction)
                    logger.info(f"[{index}/{total}] Inserted transaction {trans_id} (plate={plate}, province={province}) into Promotion Toll database and committed.")
                    break
                except Exception as e:
                    # Roll back this row's failed statement first.
                    try:
                        conn.rollback()
                    except Exception as rb_err:
                        logger.warning(f"[{index}/{total}] Rollback failed for transaction {trans_id}: {rb_err}")
                    if is_duplicate_key_error(e):
                        # The row already exists in PROMOTION_TOLL (e.g. inserted
                        # by a previous run, or a race with the existence check).
                        # Treat it as processed so the Toll DB still gets updated.
                        dup_count += 1
                        processed.append(transaction)
                        logger.info(f"[{index}/{total}] Transaction {trans_id} (plate={plate}, province={province}) already exists (duplicate key). Treating as processed.")
                        break
                    if is_deadlock_error(e) and attempt <= DEADLOCK_MAX_RETRY:
                        # Deadlock is transient: wait briefly and retry this row.
                        deadlock_retry_total += 1
                        logger.warning(f"[{index}/{total}] Deadlock inserting transaction {trans_id} (attempt {attempt}/{DEADLOCK_MAX_RETRY + 1}). Retrying in {DEADLOCK_RETRY_DELAY}s...")
                        time.sleep(DEADLOCK_RETRY_DELAY)
                        continue
                    # Real failure (or deadlock retries exhausted); retry next run.
                    failed_count += 1
                    if is_deadlock_error(e):
                        logger.error(f"[{index}/{total}] Deadlock persisted after {DEADLOCK_MAX_RETRY} retries for transaction {trans_id} (plate={plate}, province={province}): {e}")
                    else:
                        logger.error(f"[{index}/{total}] Error inserting transaction {trans_id} (plate={plate}, province={province}): {e}")
                    break
        logger.info(
            f"Insert summary (Promotion Toll DB): total={total}, inserted={inserted_count}, "
            f"already_existed={existing_count}, duplicate_key={dup_count}, failed={failed_count}, "
            f"deadlock_retries={deadlock_retry_total}, processed(safe_to_update)={len(processed)}."
        )
        if failed_count:
            logger.warning(f"{failed_count} transaction(s) failed to insert and will be retried on the next run.")

    except Exception as e:
        logger.error(f"Error inserting transactions: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()

    return processed

def update_Toll_Transactions(config, transactions):
    """
    Update specific transactions in the Toll database to set DMTPX_PROMOTION_DATE.
    """
    logger.debug(f"Using configuration: {redact_config(config)}")
    conn = None
    try:
        conn = get_Tolldb_connection(config)
        if not conn:
            logger.error("No connection to Toll database. Update aborted.")
            return

        cursor = conn.cursor()
        # Deadlock prevention: yield to the live toll system if a deadlock hits.
        apply_deadlock_prevention(cursor)
        SQL_Update = """
            UPDATE DMT_PASSING_TRANSACTION
            SET DMTPX_PROMOTION_DATE = getdate()
            WHERE DMTPX_ID = ?
            and DMTPX_TRX_DATETIME = ?
        """
        total = len(transactions)
        updated_count = 0
        notfound_count = 0
        failed_count = 0
        deadlock_retry_total = 0
        logger.info(f"Starting update of DMTPX_PROMOTION_DATE in Toll database: {total} transaction(s) to process.")

        for index, transaction in enumerate(transactions, start=1):
            dmtpx_id = getattr(transaction, 'DMTPX_ID', '?')
            trx_dt = getattr(transaction, 'DMTPX_TRX_DATETIME', '?')
            attempt = 0
            while True:
                attempt += 1
                try:
                    cursor.execute(SQL_Update, transaction.DMTPX_ID, transaction.DMTPX_TRX_DATETIME)
                    affected = cursor.rowcount
                    # Commit this single row so a later failure cannot undo it.
                    conn.commit()
                    if affected and affected > 0:
                        updated_count += 1
                        logger.debug(f"[{index}/{total}] Updated transaction DMTPX_ID={dmtpx_id} (TRX_DATETIME={trx_dt}), rows affected={affected}.")
                    else:
                        # No matching row (already updated, or datetime mismatch).
                        notfound_count += 1
                        logger.warning(f"[{index}/{total}] No matching row to update for DMTPX_ID={dmtpx_id} (TRX_DATETIME={trx_dt}); rows affected=0.")
                    break
                except Exception as e:
                    # Roll back only this row and keep going with the rest.
                    try:
                        conn.rollback()
                    except Exception as rb_err:
                        logger.warning(f"[{index}/{total}] Rollback failed for DMTPX_ID={dmtpx_id}: {rb_err}")
                    if is_deadlock_error(e) and attempt <= DEADLOCK_MAX_RETRY:
                        # Deadlock is transient: wait briefly and retry this row.
                        deadlock_retry_total += 1
                        logger.warning(f"[{index}/{total}] Deadlock updating DMTPX_ID={dmtpx_id} (attempt {attempt}/{DEADLOCK_MAX_RETRY + 1}). Retrying in {DEADLOCK_RETRY_DELAY}s...")
                        time.sleep(DEADLOCK_RETRY_DELAY)
                        continue
                    failed_count += 1
                    if is_deadlock_error(e):
                        logger.error(f"[{index}/{total}] Deadlock persisted after {DEADLOCK_MAX_RETRY} retries updating DMTPX_ID={dmtpx_id} (TRX_DATETIME={trx_dt}): {e}")
                    else:
                        logger.error(f"[{index}/{total}] Error updating transaction DMTPX_ID={dmtpx_id} (TRX_DATETIME={trx_dt}): {e}")
                    break
        logger.info(
            f"Update summary (Toll DB): total={total}, updated={updated_count}, "
            f"no_match={notfound_count}, failed={failed_count}, deadlock_retries={deadlock_retry_total}."
        )
        if failed_count:
            logger.warning(f"{failed_count} transaction(s) failed to update in the Toll database.")

    except Exception as e:
        logger.error(f"Error updating transaction: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()

def export_Transactions_CSV(transactions, columns, file_path):
    """
    Write the given transactions to a CSV file using the provided columns.
    Used by dry-run mode to preview INSERT/UPDATE data without touching the DB.
    Returns the number of rows written.
    """
    try:
        # Make sure the output directory exists.
        directory = os.path.dirname(file_path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        with open(file_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(columns)
            for transaction in transactions:
                writer.writerow([getattr(transaction, col, "") for col in columns])

        logger.info(f"[DRY-RUN] Wrote {len(transactions)} rows to {file_path}")
        return len(transactions)
    except Exception as e:
        logger.error(f"[DRY-RUN] Error writing CSV file {file_path}: {e}")
        return 0

def main(config, dry_run=False):
    """
    Main function to process transactions:
    - Fetch transactions and promotions
    - Match and insert promotions
    - Update matched transactions in Toll DB

    When dry_run is True, no INSERT/UPDATE is performed against the databases.
    Instead, the matched (would-be-inserted) and the to-be-updated transactions
    are exported to CSV files for review.
    """
    logger.debug(f"Using configuration: {redact_config(config)}")
    try:
        def normalize_text(value):
            """Return a safe normalized string: strip + lower, handling None."""
            if value is None:
                return ""
            try:
                return str(value).strip().lower()
            except Exception:
                return ""
        # Fetch transactions from Toll DB. None means the fetch errored (as
        # opposed to an empty list, which means there is genuinely nothing to
        # do). On error we abort without touching either database.
        transactions = get_Toll_Transactions(config.get('Toll_DB'))
        if transactions is None:
            logger.error("Aborting run: could not fetch transactions from the Toll database. No changes were made.")
            return
        logger.debug(f"Transactions: {transactions}")

        # Fetch promotions from Promotion DB. If this errors we MUST abort:
        # proceeding would leave promotion_keys empty, match nothing, and then
        # mark every transaction as processed in the Toll DB even though it was
        # never actually checked against the promotion register.
        promotions = get_Promotion_Register(config.get('Promotion_DB'))
        if promotions is None:
            logger.error("Aborting run: could not fetch the promotion register. Toll DB will NOT be updated, so unchecked transactions are not lost.")
            return
        logger.debug(f"Promotions: {promotions}")

        # Build a lookup set of (licence_plate, province) once so matching is
        # O(transactions) instead of O(transactions x promotions).
        promotion_keys = {
            (
                normalize_text(getattr(promotion, "LICENCE_PLATE", None)),
                normalize_text(getattr(promotion, "PROVINCE", None)),
            )
            for promotion in promotions
        }

        logger.info(
            f"Fetched {len(transactions)} transaction(s) from Toll DB and "
            f"{len(promotions)} active promotion(s) ({len(promotion_keys)} unique plate/province key(s))."
        )

        # Match transactions with promotions
        matched_transactions = []
        for transaction in transactions:
            lp = normalize_text(getattr(transaction, "DMTPX_LICENCEPLATE", None))
            prov = normalize_text(getattr(transaction, "DMTPX_PROVINCE", None))
            if lp and prov and (lp, prov) in promotion_keys:
                logger.debug(
                    f"Promotion found for licence plate {transaction.DMTPX_LICENCEPLATE} in province {transaction.DMTPX_PROVINCE} in transaction {transaction.DMTPX_ID}."
                )
                matched_transactions.append(transaction)

        logger.info(
            f"Matching complete: {len(matched_transactions)} of {len(transactions)} "
            f"transaction(s) matched an active promotion."
        )

        if dry_run:
            # Dry-run mode: export to CSV instead of writing to the databases.
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            output_dir = config.get('Dry_Run_Dir', 'dry_run_output')

            # Columns that would be INSERTed into PROMOTION_TOLL.
            insert_columns = [
                "ID", "DMTPX_TRX_DATETIME", "DMTPX_TSB_ID", "DMTPX_PLAZA_ID",
                "DMTPX_LANE_ID", "DMTPX_PRICE_IN_CURRENCY", "DMTPX_LICENCEPLATE",
                "DMTPX_PROVINCE", "DMTPX_RECEIPT_NO", "DMTPX_TC_PAYMENTMETHOD_ID",
            ]
            insert_file = os.path.join(output_dir, f"dry_run_insert_{timestamp}.csv")
            export_Transactions_CSV(matched_transactions, insert_columns, insert_file)
            logger.info(f"[DRY-RUN] {len(matched_transactions)} matched transactions would be INSERTed into the Promotion Toll database.")

            # Columns that would be UPDATEd in DMT_PASSING_TRANSACTION.
            update_columns = ["DMTPX_ID", "DMTPX_TRX_DATETIME"]
            update_file = os.path.join(output_dir, f"dry_run_update_{timestamp}.csv")
            export_Transactions_CSV(transactions, update_columns, update_file)
            logger.info(f"[DRY-RUN] {len(transactions)} transactions would be UPDATEd in the Toll database.")

            logger.info("[DRY-RUN] Completed. No changes were written to any database.")
            return

        # Insert matched transactions into Promotion Toll DB
        inserted_ok = []
        if matched_transactions:
            logger.info(f"Inserting {len(matched_transactions)} matched transactions into the Promotion Toll database.")
            inserted_ok = insert_Toll_Transactions(config.get('Promotion_DB'), matched_transactions)
            for t in inserted_ok:
                logger.debug(f"Transaction {t.DMTPX_ID} updated with promotion details.")
        else:
            logger.info("No matched transactions to insert into the Promotion Toll database.")

        # Work out which matched transactions failed to insert. Those must NOT
        # be marked as processed in the Toll DB, otherwise they would never be
        # picked up (and inserted) again on a later run.
        inserted_ids = {t.ID for t in inserted_ok}
        failed_ids = {t.ID for t in matched_transactions} - inserted_ids
        if failed_ids:
            logger.warning(
                f"{len(failed_ids)} matched transactions failed to insert into the Promotion "
                f"Toll database and will NOT be marked as processed (will retry next run)."
            )

        # Update the original transactions in the Toll database, skipping the
        # matched ones that failed to insert above.
        transactions_to_update = [t for t in transactions if t.ID not in failed_ids]
        # Deadlock prevention: always acquire row locks in a consistent order
        # (by primary key DMTPX_ID) so concurrent runs/sessions cannot form a
        # lock cycle with each other.
        try:
            transactions_to_update.sort(key=lambda t: getattr(t, "DMTPX_ID", 0))
        except Exception as sort_err:
            logger.warning(f"Could not sort transactions for update ordering: {sort_err}")
        skipped = len(transactions) - len(transactions_to_update)
        logger.info(
            f"Preparing Toll DB update: {len(transactions_to_update)} transaction(s) will be marked as processed, "
            f"{skipped} skipped due to failed insert."
        )
        if transactions_to_update:
            update_Toll_Transactions(config.get('Toll_DB'), transactions_to_update)
        else:
            logger.info("No transactions to update in the Toll database.")

        logger.info("All transactions processed successfully.")
    except Exception as e:
        logger.error(f"Error processing transactions in main: {e}")
        return 

if __name__ == "__main__":

    # Dry-run mode: run a single pass and export CSV files instead of
    # writing INSERT/UPDATE to the databases. Enabled via the "Dry_Run" config
    # key; the --dry-run / -d command-line flag also forces it on.
    dry_run = str(config.get('Dry_Run', 0)) == "1" \
        or any(arg in ("--dry-run", "-d") for arg in sys.argv[1:])

    if dry_run:
        logger.info("Starting in DRY-RUN mode: a single pass will run and CSV files will be generated instead of writing to the databases.")
        try:
            main(config, dry_run=True)
        except KeyboardInterrupt:
            logger.info("Dry-run interrupted by user. Shutting down.")
        except Exception as e:
            logger.critical(f"An unhandled exception occurred during the dry-run: {e}", exc_info=True)
        sys.exit(0)

    while True:
        try:
            main(config)
            retry_interval = config.get('RETRY_INTERVAL', 10)
            logger.info(f"Run complete. Waiting for {retry_interval} seconds before the next run.")
            time.sleep(retry_interval)
        except KeyboardInterrupt:
            logger.info("Processing interrupted by user. Shutting down.")
            break
        except Exception as e:
            logger.critical(f"An unhandled exception occurred in the main loop: {e}", exc_info=True)
            time.sleep(60)