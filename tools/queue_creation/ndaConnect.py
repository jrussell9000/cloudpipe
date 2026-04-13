import boto3
import config
import logging
import pandas as pd
import re
from sqlalchemy import (
    Table,
    Column,
    String,
    Integer,
    MetaData,
    create_engine
)

'''
### If an error about OptionEngine (or something) is returned, upgrade pandas###
This module takes in the connection information for a miNDAR and uses
SQLAlchemy to create a connection to the associated database instance on NDA.

From the 'Data Packages' page on the NDA User Dashboard choose a package from
the table at the bottom (make sure its status is 'Ready to Download'), then click
on the 'Actions' drop down in its row and select 'View Connection Details'. The
User Name, password (created with the miNDAR), and host need to be passed to the class below.
The port and service name are hardcoded, since they always seem to be 1521 and ORCL.

We'll use that info to craft a connection string (self.connection) for an oracle/oracle-db
SQL database. Then (create_query) we'll tell SQLAlchemy about the data table we're going to query.
We only need to tell SQLAlchemy about the table columns we're interested in for the query.
We'll grab the 'file_source' column since it contains the filename for each scan
which includes the subjectkey and the timepoint, as well as the qc_outcome. The
'fmriresults01_id' column is just a numeric id (which Dask needs). We'll craft our query
from that table to select ony the rows where qc_outcome == 'pass'. Then we'll tell Dask
to read that query into a dask dataframe (40 partitions seems fastest).

Next, we'll break apart 'file_source' into separate columns for subjectkey and time, recode
time as numeric, then sort the dataframe by subjectkey, then time, then return it.

To get a listing of all the columns (and their types) in all the tables in the miNDAR database
(i.e., to customize create_query):
from sqlalchemy import inspect, create_engine
connection = .... (see self.connection below)
engine = create_engine(connection)
inspector = inspect(engine)
for table in inspector.get_table_names():
    print(inspector.get_columns(table))
'''


class SQL_Query():

    def __init__(self):

        self.miNDAR_username= config.miNDAR_username
        self.miNDAR_password = config.miNDAR_password
        self.miNDAR_host = config.miNDAR_host
        self.miNDAR_tablename = config.miNDAR_tablename
        self.checks3: bool = False
        self.aws_access_key_id = config.aws_access_key_id
        self.aws_secret_access_key = config.aws_secret_access_key
        self.aws_s3bucket = config.aws_s3bucket
        self.aws_region_name = config.aws_region

        userpass_comb = ":".join([self.miNDAR_username, self.miNDAR_password])
        self.connection = "".join(["oracle+oracledb://", userpass_comb,
                                   "@", self.miNDAR_host, ":1521/ORCL"])
        self.miNDAR_tablename = self.miNDAR_tablename

    def create_query(self):
        # Tell SQLAlchemy about the table we're going to query by giving the name and columns (with types)
        fmriresults01 = Table("fmriresults01", MetaData(),
                              Column("fmriresults01_id", Integer, primary_key=True),
                              Column("file_source", String(1024)),
                              Column("qc_outcome", String))
        # Craft a query to select only the rows where qc_outcome == 'pass'
        query = fmriresults01.select().where(fmriresults01.c.qc_outcome == "pass")
        return (query)

    # Run the query and read the results into a pandas dataframe (dask would be quicker, but isn't playing well with SQLAlchemy)
    def query2dataframe(self, query):
        engine_cloud = create_engine(self.connection)
        with engine_cloud.connect() as conn:
            raw_df = pd.read_sql_query(query, conn, index_col="_".join(["fmriresults01", "id"]))
        return raw_df

    # Getting a list of completed subject_ses output archive files already in the S3 bucket
    def getCompleted(self):
        logging.getLogger('Removing Completed Subjects/Sessions')
        # Connect to an S3 resource
        s3 = boto3.resource(service_name='s3', region_name=self.aws_region_name,
                            aws_access_key_id=self.aws_access_key_id,
                            aws_secret_access_key=self.aws_secret_access_key)
        # Specify an S3 bucket
        bucket = s3.Bucket(self.aws_s3bucket)
        # Create a dictionary of objects in the bucket
        objects = bucket.objects.all()
        # Create a list of subject_ses pairings that are 'done'
        doneBIDSxz = [object.key for object in objects]
        # Remove the BIDS formatting and the file suffix (to allow compairson with raw NDA subj_ses)
        done = [re.sub("(sub-)|(ses-)|(\.tar\.xz)", "", subjsesXZ) for subjsesXZ in doneBIDSxz]
        # Convert to a pandas series (makes things easier down the line)
        done = pd.Series(done).str.replace('_seg','')
        return (done)

    def formatDataframe(self, raw_df: pd.DataFrame, done=None):
        # Extract the "file_source" column from the table received from the SQL query to NDA
        df = raw_df["file_source"]
        # Split the "file_source" column by "/" and retain the last value (the filename)
        df = df.str.split("/").str[-1]
        # Split the new "filename" by "_" 
        df = df.str.split("_", expand=True)
        # Rename the columns as appropriate
        df.rename(
            columns={0: "subjectkey", 1: "TimeTxt", 2: "Type", 3: "Timestamp"}, inplace=True
        )
        # Create a numeric 'Time' column recoded from 'TimeTxt' and convert it to integer type
        df["Time"] = df["TimeTxt"].replace(
            {"baselineYear1Arm1": "0", "2YearFollowUpYArm1": "2", "4YearFollowUpYArm1": "4"}
        )
        df["Time"] = df["Time"].astype(int)
        # Sort the dataframe by 'subjectkey' and 'Time', then return it inplace.
        df.sort_values(["subjectkey", "Time"], axis=0, inplace=True)
        # Create a new column called 'rawsubjses_strs' which combines the subjectkey and 
        # timepoint (e.g., NDARXXXXX_baselineYear1Arm1)
        df["rawsubjses_strs"] = df["subjectkey"] + "_" + df["TimeTxt"]
        # If a pandas series of completed subject_ses outputs was passed 
        # (from getCompleted() above), remove those from the dataframe
        if done is not None:
            df = df[~df.isin(done)]
        # Subset the dataframe to include only the rawsubjses_strs, then reset the index and drop any duplicates
        df = df["rawsubjses_strs"].reset_index(drop=True)
        df = df.drop_duplicates()
        return (df)

    def main(self):
        query = self.create_query()
        raw_df = self.query2dataframe(query)
        if self.checks3:
            df_done = self.getCompleted()
            df_done_len = len(df_done)
            print(f'Found {df_done_len} completed scans in S3 storage.')
            df = self.formatDataframe(raw_df, df_done)
        else:
            df = self.formatDataframe(raw_df)
        return (df)
