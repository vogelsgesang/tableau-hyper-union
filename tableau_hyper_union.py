import argparse
import logging, traceback
import logging.handlers # For RotatingFileHandler
import os, sys, datetime, time, glob, tempfile, re
from pathlib import Path
import tableauhyperapi as THA

# Timing
start_time = time.time()
start_datetime = datetime.datetime.utcnow()

# Logging
logger = logging.getLogger() # Root logger
logger.setLevel(logging.INFO)
log_formatter = logging.Formatter("%(asctime)s [%(levelname)s]  %(message)s")
log_console_handler = logging.StreamHandler(sys.stdout)
log_console_handler.setFormatter(log_formatter)
logger.addHandler(log_console_handler)

# Command line arguments
parser = argparse.ArgumentParser(prog="tableau_hyper_union.py", description="Unions all .hyper files in the directory it's run from, into a single Hyper extract. Documentation available at: https://github.com/biztory/tableau-hyper-union")
parser.add_argument("--output-file", "-o", dest="output_file", required=False, default="union.hyper", type=str, help="The file to output to. Defaults to \"union.hyper\".")
parser.add_argument("--preserve-output-file", "-p", dest="preserve_output_file", default=False, action="store_true", help="When this argument is specified, the script will preserve the output of the output file and append to the existing contents. When not specified (i.e. the default behavior), it will first clear the contents of said output file.")
parser.add_argument("--source-file-column-name", "-c", dest="source_file_column_name", default="source_file", type=str, help="Used to add a column to each table, containing the name of the Hyper file the data was sourced from. The column can be omitted altogether by specifying an empty string here: \"\". Otherwise, the default is \"source_file\".")
parser.add_argument("--log-to-file", dest="log_to_file", default=False, action="store_true", help="Log the output of the program to a log file, and not just to the console. Useful for when the tool is used on a schedule.")
parser.add_argument("--debug", dest="debug", default=False, action="store_true", help="Set the logging level to DEBUG, for additional output helpful for troubleshooting.")
args = parser.parse_args()

# Logs directory
logs_directory = Path("logs").absolute()
if not logs_directory.exists() and args.log_to_file:
    os.makedirs(logs_directory)

# More logging, or not?
if args.log_to_file:
    log_file = logs_directory / "tableau_hyper_union.log"
    log_file_handler = logging.handlers.RotatingFileHandler(log_file, maxBytes=5000000, backupCount=5)
    log_file_handler.setFormatter(log_formatter)
    logger.addHandler(log_file_handler)

if args.debug:
    logger.setLevel(logging.DEBUG)

# Let's go
logger.info("Biztory tableau_hyper_union.py v0.2")
logger.info("Author: Timothy Vermeiren")
logger.info(f"Script launched using (quotes removed): { sys.executable } { sys.argv[0] } { ' '.join([a for i, a in enumerate(sys.argv[1:])]) }")

if not args.preserve_output_file:
    worklist = [hyper_file for hyper_file in glob.glob("*.hyper") if hyper_file != args.output_file]
    output_file = args.output_file
else:
    output_file = args.output_file.split(".")[-1] + "_temp.hyper"
    worklist = [hyper_file for hyper_file in glob.glob("*.hyper") if hyper_file != output_file]
    # We need a temporary output file if we're going to include it in the source itself
logger.info(f"Assimilated { len(worklist) } Hyper files to be processed.")

output_dict = {} # "Structure" is a dict of dicts of dicts going schema > table > column. Used mostly for comparing.

if args.log_to_file:
    hyper_process_parameters = { "log_dir": str(logs_directory) }
else:
    # We need to log the Hyper output _somewhere_
    temp_log_dir = tempfile.gettempdir()
    hyper_process_parameters = { "log_dir": str(temp_log_dir) }

with THA.HyperProcess(telemetry=THA.Telemetry.SEND_USAGE_DATA_TO_TABLEAU, parameters=hyper_process_parameters) as hyper:

    # Pre-process the files, so we know which schemas and tables we need to go through. We also need to know about columns, because UNION might complain otherwise

    for hyper_file in worklist:
        try:
            with THA.Connection(endpoint=hyper.endpoint, database=hyper_file) as connection:
                logger.info(f"Assimilating database/file { hyper_file }:")

                # Schemas are the top level
                for schema in connection.catalog.get_schema_names():
                    logger.info(f"\t- Schema { schema }:")
                    if schema not in output_dict:
                        # SchemaDefinition as key, because why not
                        output_dict[schema] = {}

                    # Then tables
                    for table in connection.catalog.get_table_names(schema=schema):
                        logger.info(f"\t\t- Table { table }")
                        if table not in output_dict[schema]:
                            # TableDefinition as key, because why not. We'll make this a list though because its contents, column, is at the lowest level
                            output_dict[schema][table] = []
                        table_definition = connection.catalog.get_table_definition(name=table)
                        logger.info(f"\t\t\t{ table_definition.column_count } columns in table.")

                        # Then columns
                        for column in table_definition.columns:
                            logger.debug(f"\t\t\t- Column {column.name} has type={column.type} and nullability={column.nullability}")
                            # If the column doesn't exist, we simply add it
                            if column.name not in [column.name for column in output_dict[schema][table]]:
                                output_dict[schema][table].append(column) # Uh, okay.
                            else:
                                # Two possibilities...
                                matching_column_in_output_dict = [column_output for column_output in output_dict[schema][table] if column_output.name == column.name][0]
                                if column.type != matching_column_in_output_dict.type or column.nullability != matching_column_in_output_dict.nullability or column.collation != matching_column_in_output_dict.collation:
                                    # If it does exist but doesn't match data type or nullability, we'll be in trouble. Simply discard it for now, we can think of alternative approaches later
                                    logger.warning(f"\t\t\tFound matching column { column }, but it doesn't have the same properties as the existing column. This might cause unexpected results in the output, such as missing data or a general failure. (Existing: { matching_column_in_output_dict.type }/{ matching_column_in_output_dict.nullability }/{matching_column_in_output_dict.collation}) != ({hyper_file}: { column.type }/{ column.nullability }/{column.collation})")
                                else:
                                    logger.debug("Column exists in output already.")
        except Exception as e:
            logger.error(f"There was a problem reading data from the file { hyper_file }. The error returned was:\n\t{e}\n\t{traceback.format_exc()}")
            input("Press Enter to continue...")

        logger.info("The connection to the Hyper file has been closed.")
    
    logger.info("Assimilated aforementioned files. Creating definitions and applying in output file.")

    try:
        # Remove the output file in any case
        if os.path.exists(output_file):
            os.remove(output_file)

        # We do not specify a database, because we'll connect to ("attach") all input files as well as the output file at once.
        with THA.Connection(endpoint=hyper.endpoint) as connection:

            # Preparation of the output and the inputs
            connection.catalog.create_database(database_path=output_file)
            connection.catalog.attach_database(database_path=output_file, alias="union_output")
            for file in worklist:
                connection.catalog.attach_database(database_path=file)

            for schema in output_dict:

                # Now refer to this schema as part of the output
                output_schema = THA.SchemaName("union_output", schema)
                connection.catalog.create_schema_if_not_exists(schema=output_schema)

                for table in output_dict[schema]:
                
                    union_query = f"CREATE TABLE \"union_output\".{ THA.escape_name(schema.name) }.{ THA.escape_name(table.name) } AS\n"

                    for file in worklist:

                        file_database_name = file.split(".")[:-1][0]
                        schema_input = THA.SchemaName(file_database_name, table.schema_name)

                        # Comparing tuples is easier as it omits the database name, TableName() doesn't.
                        if (table.schema_name.name, table.name) in [(table_input.schema_name.name, table_input.name) for table_input in connection.catalog.get_table_names(schema=schema_input)]:

                            table_input = THA.TableName(file_database_name, table.schema_name, table.name)

                            # We do not create the table and its definitions beforehand; it is cumbersome. Rather, we'll use CREATE ... AS with the output from the UNION query we put together
                            try:
                                union_query += "SELECT"
                                for column in output_dict[schema][table]:
                                    if column.name in [column.name for column in connection.catalog.get_table_definition(name=table_input).columns] and THA.escape_name(column.name) != THA.escape_name(args.source_file_column_name):
                                        # Source extract has this column (too)
                                        union_query += f" { THA.escape_name(column.name) },"
                                    elif THA.escape_name(column.name) != THA.escape_name(args.source_file_column_name):
                                        # Source extract does not have this column
                                        union_query += f" NULL as { THA.escape_name(column.name) },"
                                if len(args.source_file_column_name) > 0: # If we must add the file name
                                    if file != args.output_file:
                                        union_query += f" '{ file }' as { args.source_file_column_name },"
                                    else:
                                        union_query += f" { THA.escape_name(args.source_file_column_name) } as { args.source_file_column_name },"
                                # Pinch off the last comma we added (I know, it's dumb, but it works)
                                union_query = union_query[:-1]
                                union_query += f" FROM { THA.escape_name(file_database_name) }.{ THA.escape_name(schema.name) }.{ THA.escape_name(table_input.name) }\nUNION ALL\n"
                            except Exception as e:
                                logger.error(f"There was a problem building the query to read the data from table { table } in file { file }. The error returned was:\n\t{e}\n\t{traceback.format_exc()}")
                                if union_query in locals():
                                    logger.error(f"The query we built so far was:\n\t{ union_query }")
                                input("Press Enter to continue...")
                        
                        else:
                            logger.info(f"Table { table.name } is not present in file { file }; omitting it from the UNION query.")
                    
                    # Remove the last UNION ALL, if there is one. Super dumb, but probably the easiest way.
                    if union_query.endswith(f"UNION ALL\n"):
                        union_query = union_query[:-10]

                    logger.debug(f"Resulting query for table { table }:\n{ union_query }")
                    logger.info(f"Performing UNION ALL for {schema.name}.{table.name}.")
                    # "Process" this table
                    connection.execute_command(union_query)

        if output_file != args.output_file:
            logger.info("We wrote the output to a temporary file to also assimilate the original output file's content. Cleaning up.")
            os.remove(args.output_file)
            os.rename(output_file, args.output_file)

    except Exception as e:
        logger.error(f"There was a problem accessing/creating/approaching the output file { args.output_file }. Perhaps the file is still open in another process, potentially Tableau? The error returned was:\n\t{e}\n\t{traceback.format_exc()}")
        input("Press Enter to continue...")

logger.info(f"The Hyper process has been shut down. Hypa hypaaaa!")

# Hyper Hyper: https://www.youtube.com/watch?v=7Twnmhe948A
# But also, Hypa Hypa: https://www.youtube.com/watch?v=75Mw8r5gW8E