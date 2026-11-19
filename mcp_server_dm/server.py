import os
import logging
import dmPython
from fastmcp import FastMCP
import sys

# Setup logging with more detailed format
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stderr)
    ]
)
logger = logging.getLogger("mcp_dameng")


# Initialize FastMCP with error handling
try:
    mcp = FastMCP("dameng")
    logger.info("FastMCP server initialized successfully")
except Exception as e:
    logger.error(f"Failed to initialize FastMCP: {e}")
    sys.exit(1)

def get_connection():
    try:
        host = os.environ.get("DM_HOST", "localhost")
        port = int(os.environ.get("DM_PORT", "5236"))
        user = os.environ.get("DM_USERNAME", "SYSDBA")
        password = os.environ.get("DM_PASSWORD", "SYSDBA")
        
        logger.info(f"Connecting to DM database at {host}:{port} as {user}")
        
        conn = dmPython.connect(
            user=user,
            password=password,
            server=host,
            port=port
        )
        return conn
    except Exception as e:
        logger.error(f"Failed to connect to database: {e}")
        raise

@mcp.tool()
def list_tables() -> str:
    """List all tables in the database."""
    logger.info("Listing tables")
    conn = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # Switch schema if provided
        schema = os.environ.get("DM_SCHEMA")
        if schema:
            try:
                cursor.execute(f"SET SCHEMA {schema}")
            except Exception as e:
                logger.warning(f"Could not set schema to {schema}: {e}")
            
            # If schema is provided, we should look for tables in that schema (Owner)
            cursor.execute(f"SELECT TABLE_NAME FROM ALL_TABLES WHERE OWNER = '{schema}'")
        else:
            # Otherwise list tables for the current user
            cursor.execute("SELECT TABLE_NAME FROM USER_TABLES")
            
        rows = cursor.fetchall()
        
        tables = [row[0] for row in rows]
        
        return "\n".join(tables) if tables else "No tables found."
        
    except Exception as e:
        logger.error(f"Error listing tables: {e}")
        return f"Error: {str(e)}"
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass

@mcp.tool()
def execute_query(query: str) -> str:
    """Execute a read-only SQL query (SELECT, SHOW, DESCRIBE, EXPLAIN)."""
    logger.info(f"Executing query: {query}")
    
    # Basic security check for read-only
    query_upper = query.strip().upper()
    allowed_starts = ["SELECT", "SHOW", "DESCRIBE", "EXPLAIN"]
    if not any(query_upper.startswith(start) for start in allowed_starts):
        return "Error: Only read-only queries (SELECT, SHOW, DESCRIBE, EXPLAIN) are allowed."
    
    conn = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        schema = os.environ.get("DM_SCHEMA")
        if schema:
            try:
                cursor.execute(f"SET SCHEMA {schema}")
            except Exception as e:
                logger.warning(f"Could not set schema to {schema}: {e}")
            
        cursor.execute(query)
        
        if cursor.description:
            columns = [desc[0] for desc in cursor.description]
            results = cursor.fetchall()
            
            output = [f"Columns: {', '.join(columns)}"]
            for row in results:
                output.append(str(row))
            
            return "\n".join(output)
        else:
            return "Query executed successfully (no results)."
            
    except Exception as e:
        logger.error(f"Error executing query: {e}")
        return f"Error: {str(e)}"
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass

@mcp.tool()
def describe_table(table_name: str) -> str:
    """Show the structure of a specified table."""
    logger.info(f"Describing table: {table_name}")
    conn = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        schema = os.environ.get("DM_SCHEMA")
        if schema:
            try:
                cursor.execute(f"SET SCHEMA {schema}")
            except Exception as e:
                logger.warning(f"Could not set schema to {schema}: {e}")
        
        # Query column information
        # Using USER_TAB_COLUMNS is standard in Oracle-like DBs (Dameng is similar)
        if schema:
            sql = f"SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH, NULLABLE FROM ALL_TAB_COLUMNS WHERE TABLE_NAME = '{table_name.upper()}' AND OWNER = '{schema}'"
        else:
            sql = f"SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH, NULLABLE FROM USER_TAB_COLUMNS WHERE TABLE_NAME = '{table_name.upper()}'"
        
        cursor.execute(sql)
        columns = cursor.fetchall()
        
        if not columns:
            return f"Table '{table_name}' not found or has no columns."

        output = [f"Table: {table_name}", "Column | Type | Length | Nullable", "-" * 40]
        for col in columns:
            output.append(f"{col[0]} | {col[1]} | {col[2]} | {col[3]}")
            
        return "\n".join(output)
    except Exception as e:
        logger.error(f"Error describing table: {e}")
        return f"Error: {str(e)}"
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass

if __name__ == "__main__":
    mcp.run()
