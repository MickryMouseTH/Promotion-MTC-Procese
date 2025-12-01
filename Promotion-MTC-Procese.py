from LogLibrary import Load_Config, Loguru_Logging
import pyodbc
import time
import json
import os
import sys

# ----------------------- Configuration Values -----------------------
Program_Name = "Promotion-MTC-Procese"
Program_Version = "1.2"
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
    "log_Level": "DEBUG",
    "Log_Console": 1,         # Set to "true" to enable console logging.
    "log_Backup": 90,         # Log retention duration (number of backup files).
    "Log_Size": "10 MB"       # Maximum log file size before rotation. 
}

config = Load_Config(default_config, Program_Name)
logger = Loguru_Logging(config, Program_Name, Program_Version)

def get_Tolldb_connection(config):
    """
    Create and return a database connection object for the Toll database.
    """
    logger.debug(f"Using configuration: {config}")
    try:
        conn_str = (
            f"DRIVER={config.get('ODBC_Driver', 'ODBC Driver 18 for SQL Server')};"
            f"SERVER={config.get('DB_SERVER')};"
            f"DATABASE={config.get('DB_DATABASE')};"
            f"UID={config.get('DB_USERNAME')};"
            f"PWD={config.get('DB_PASSWORD')};"
            "TrustServerCertificate=yes;"
        )
        logger.debug(f"Connection string: {conn_str}")
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
    logger.debug(f"Using configuration: {config}")
    try:
        conn_str = (
            f"DRIVER={config.get('ODBC_Driver', 'ODBC Driver 17 for SQL Server')};"
            f"SERVER={config.get('DB_SERVER')};"
            f"DATABASE={config.get('DB_DATABASE')};"
            f"UID={config.get('DB_USERNAME')};"
            f"PWD={config.get('DB_PASSWORD')};"
            "TrustServerCertificate=yes;"
        )
        logger.debug(f"Connection string: {conn_str}")
        conn = pyodbc.connect(conn_str)
        logger.info("Promotion database connection established successfully!")
        return conn
    except Exception as e:
        logger.error(f"Failed to connect to Promotion database: {e}")
        return None
       
def get_Toll_Transactions(config):
    """
    Fetch transactions from the Toll database.
    Returns a list of transaction rows.
    """
    logger.debug(f"Using configuration: {config}")
    try:
        logger.info("Fetching transactions from the Toll database...")
        conn = get_Tolldb_connection(config)
        if not conn:
            logger.error("No connection to Toll database. Returning empty list.")
            return []

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
            AND (dpt.DMTPX_LICENCEPLATE IS NOT NULL or dpt.DMTPX_LICENCEPLATE <> '')
            AND (dpt.DMTPX_PROVINCEID IS NOT NULL or dpt.DMTPX_PROVINCEID <> '')
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
        return []
    
def get_Promotion_Register(config):
    """
    Fetch promotion register from the Promotion database.
    Returns a list of promotion rows.
    """
    logger.debug(f"Using configuration: {config}")
    try:
        logger.info("Fetching promotion register from the Promotion database...")
        conn = get_Promotiondb_connection(config)
        if not conn:
            logger.error("No connection to Promotion database. Returning empty list.")
            return []

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
        return []
    
def insert_Toll_Transactions(config, transactions):
    """
    Insert transactions into the Promotion Toll database.
    Skips transactions that already exist (by TRANS_NO).
    """
    logger.debug(f"Using configuration: {config}")

    SQL_Insert = """
    INSERT INTO PROMOTION_TOLL
          (TRANS_NO, TRX_DATETIME, TSB_ID, PLAZA_ID, LANE_ID, PRICE, LICENCE_PLATE, PROVINCE, RECEIPT_NO, PAYMENTMETHOD_ID, STATUS, CREATE_BY, CREATE_DATETIME )
    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'W', '7710599', getdate());
    """
    SQL_Check = "SELECT COUNT(1) FROM PROMOTION_TOLL WHERE TRANS_NO = ?"

    try:
        conn = get_Promotiondb_connection(config)
        if not conn:
            logger.error("No connection to Promotion database. Insert aborted.")
            return

        cursor = conn.cursor()
        inserted_count = 0
        for transaction in transactions:
            cursor.execute(SQL_Check, transaction.ID)
            exists = cursor.fetchone()[0]
            if exists:
                logger.info(f"Transaction {transaction.ID} already exists. Skipping insert.")
                continue
            cursor.execute(SQL_Insert, transaction.ID, transaction.DMTPX_TRX_DATETIME, transaction.DMTPX_TSB_ID, transaction.DMTPX_PLAZA_ID, transaction.DMTPX_LANE_ID, transaction.DMTPX_PRICE_IN_CURRENCY, transaction.DMTPX_LICENCEPLATE, transaction.DMTPX_PROVINCE, transaction.DMTPX_RECEIPT_NO, transaction.DMTPX_TC_PAYMENTMETHOD_ID)
            inserted_count += 1
            logger.debug(f"Inserted transaction {transaction.ID} into Promotion Toll database.")
        conn.commit()
        logger.info(f"Inserted {inserted_count} new transactions into the Toll database.")
        cursor.close()
        conn.close()

    except Exception as e:
        logger.error(f"Error inserting transactions: {e}")

def update_Toll_Transactions(config, transactions):
    """
    Update specific transactions in the Toll database to set DMTPX_PROMOTION_DATE.
    """
    logger.debug(f"Using configuration: {config}")
    try:
        conn = get_Tolldb_connection(config)
        if not conn:
            logger.error("No connection to Toll database. Update aborted.")
            return

        cursor = conn.cursor()
        SQL_Update = """
            UPDATE DMT_PASSING_TRANSACTION
            SET DMTPX_PROMOTION_DATE = getdate()
            WHERE DMTPX_ID = ?
            and DMTPX_TRX_DATETIME = ?
        """
        updated_count = 0
        for transaction in transactions:
            cursor.execute(SQL_Update, transaction.DMTPX_ID, transaction.DMTPX_TRX_DATETIME)
            updated_count += 1
            #logger.info(f"Transaction {transaction.DMTPX_ID} updated successfully.")
        conn.commit()
        cursor.close()
        conn.close()
        logger.info(f"Updated {updated_count} transactions in the Toll database.")

    except Exception as e:
        logger.error(f"Error updating transaction: {e}")

def main(config):
    """
    Main function to process transactions:
    - Fetch transactions and promotions
    - Match and insert promotions
    - Update matched transactions in Toll DB
    """
    logger.debug(f"Using configuration: {config}")
    try:
        def normalize_text(value):
            """Return a safe normalized string: strip + lower, handling None."""
            if value is None:
                return ""
            try:
                return str(value).strip().lower()
            except Exception:
                return ""
        # Fetch transactions from Toll DB
        transactions = get_Toll_Transactions(config.get('Toll_DB'))
        #logger.info(f"Fetched {len(transactions)} transactions from the Toll database.")
        logger.debug(f"Transactions: {transactions}")

        # Fetch promotions from Promotion DB
        promotions = get_Promotion_Register(config.get('Promotion_DB'))
        #logger.info(f"Fetched {len(promotions)} promotions from the Promotion database.")
        logger.debug(f"Promotions: {promotions}")

        # Match transactions with promotions
        matched_transactions = []
        for transaction in transactions:
            lp = normalize_text(getattr(transaction, "DMTPX_LICENCEPLATE", None))
            prov = normalize_text(getattr(transaction, "DMTPX_PROVINCE", None))
            for promotion in promotions:
                promo_lp = normalize_text(getattr(promotion, "LICENCE_PLATE", None))
                promo_prov = normalize_text(getattr(promotion, "PROVINCE", None))
                if lp and prov and (lp == promo_lp) and (prov == promo_prov):
                    logger.debug(
                        f"Promotion found for licence plate {transaction.DMTPX_LICENCEPLATE} in province {transaction.DMTPX_PROVINCE} in transaction {transaction.DMTPX_ID}."
                    )
                    matched_transactions.append(transaction)
                    break  # Stop checking other promotions if matched

        # Insert matched transactions into Promotion Toll DB
        if matched_transactions:
            logger.info(f"Inserting {len(matched_transactions)} matched transactions into the Promotion Toll database.")
            insert_Toll_Transactions(config.get('Promotion_DB'), matched_transactions)
            for t in matched_transactions:
                logger.debug(f"Transaction {t.DMTPX_ID} updated with promotion details.")
        else:
            logger.info("No matched transactions to insert into the Promotion Toll database.")

        # Update the original transactions in the Toll database
        if transactions:    
            logger.info(f"Updating {len(transactions)} transactions in the Toll database.")
            update_Toll_Transactions(config.get('Toll_DB'), transactions)

        logger.info("All transactions processed successfully.")
    except Exception as e:
        logger.error(f"Error processing transactions in main: {e}")
        return 

if __name__ == "__main__":
    
    while True:
        try:
            main(config)
            logger.info(f"Run complete. Waiting for {config.get('RETRY_INTERVAL', 10)} seconds before the next run.")
            time.sleep(config.get('RETRY_INTERVAL', 3600))
        except KeyboardInterrupt:
            logger.info("Processing interrupted by user. Shutting down.")
            break
        except Exception as e:
            logger.critical(f"An unhandled exception occurred in the main loop: {e}", exc_info=True)
            time.sleep(60)