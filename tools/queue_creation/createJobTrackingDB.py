import boto3
import numpy as np
import os
import pandas as pd
import random
import sys
from botocore.exceptions import ClientError
from boto3.dynamodb.types import TypeSerializer

from dataclasses import dataclass

# setting path
sys.path.append(f'{os.path.dirname(os.path.realpath(__file__))}/..')
from batched import config, ndaConnect


# Cannibalizing code from https://github.com/DrGFreeman/dynamo-pandas/blob/main/dynamo_pandas/transactions/transactions.py

@dataclass
class pullABCD_IDs:

    session: boto3.Session
    tableName: str
    append: bool = False

    def __post_init__(self):
        if self.append:
            self.tableName = "_".join([self.tableName, random.randrange(999)])
        self.main()

    # Get single column pandas dataframe of non-duplicating ABCD subject keys and timepoints (subj_timepoint)
    def getScanIDs(self) -> pd.DataFrame:
        try:
            print('Collecting ABCD IDs from NDA into a dataframe...')
            SQL = ndaConnect.SQL_Query()
            df = SQL.main()
        except Exception as exc:
            raise exc
        df = df.to_frame()
        return df

    # Add columns to the dataframe that we'll need in the tracking database
    # Pandas will automagically select the data type
    def addProcColumns(self, df: pd.DataFrame) -> pd.DataFrame:
        print('Adding columns to the dataframe...')
        df.rename(columns={'rawsubjses_strs': 'subject_timepoint'}, inplace=True)
        # Additional table columns can be added here with df.insert([Index], [Name], [Default Value])
        df.insert(1, 'InProcessing', 0)
        df.insert(2, 'Segmented', 0)
        df.insert(3, 'Parcellated', 0)

        # 'Chunking' the workload by creating a batch index
        # that will assign a common value to 'batches' of jobs (e.g., 10)
        i = 0
        while i < (len(df) // config.batch_size):
            single = np.repeat(i, config.batch_size)
            if i == 0:
                batch = single
            else:
                batch = np.append(batch, single)
            i += 1
        batch = np.append(batch, np.repeat(i, len(df) % config.batch_size))
        df.insert(0, 'BatchIndex', batch)
        df.sort_values(by='BatchIndex', inplace=True)

        return df

    def python_to_dynamo(self, df: pd.DataFrame) -> dict:
        """
        Converts the incoming Pandas dataframe into a 'records' format dictionary.
        Then uses the boto3.dynamodb TypeSerializer class to convert the dictionary
        into a list of item records, each in DynamoDB-format JSON (i.e., a JSON format 
        that can be imported into a DynamoDB table).

        :param df: A Pandas dataframe containing the table data
        :return ddb_json_list: A list of DynamoDB JSON-formatted records (row data) from the Pandas dataframe
        """
        print('Converting data frame to DynamoDB JSON...')
        ts = TypeSerializer()
        dict_list = df.to_dict("records")  # ‘records’ : list-like [{column -> value}, … , {column -> value}] (output is list, not dict)
        ddb_json_list = [i["M"] for i in ts.serialize(dict_list)["L"]]
        return (ddb_json_list)

    def createDDBTable(self, tableName):
        """
        Creates an Amazon DynamoDB table that can be used to store movie data.
        The table uses the release year of the movie as the partition key and the
        title as the sort key.

        :param tableName: The name of the table to create.
        :return: The newly created table.
        """
        try:
            print('Creating empty DynamoDB table...')
            self.table = self.session.resource('dynamodb').create_table(
                TableName=tableName,
                KeySchema=[
                    {'AttributeName': 'BatchIndex', 'KeyType': 'HASH'},
                    {'AttributeName': 'subject_timepoint', 'KeyType': 'RANGE'}],
                AttributeDefinitions=[
                    {'AttributeName': 'BatchIndex', 'AttributeType': 'N'},
                    {'AttributeName': 'subject_timepoint', 'AttributeType': 'S'}],
                ProvisionedThroughput={
                    "ReadCapacityUnits": 100,
                    "WriteCapacityUnits": 100,
                },
            )
            self.table.wait_until_exists()

        except ClientError as err:
            print(err.response["Error"]["Code"])
            print(err.response["Error"]["Message"])
            raise
        else:
            # This return is currently unnecessary, though could be passed as input
            # to write2DDBTable
            return self.table

    def batchwrite(self, items, tableName):

        dynamodb = self.session.client("dynamodb")

        response = dynamodb.batch_write_item(
            RequestItems={tableName: [{"PutRequest": {"Item": item}} for item in items]}
        )

        if response["UnprocessedItems"] != {}:
            return [
                item["PutRequest"]["Item"] for item in response["UnprocessedItems"][tableName]
            ]
        else:
            return []

    def write2DDBTable(self, ddb_json_list: list, tableName: str):
        print('Writing to the job tracking table...')

        # The size of the item batches (# of rows) that will be written to the table at each pass
        batch_size = 25

        while len(ddb_json_list) > 0:
            batch_items = ddb_json_list[:batch_size]
            ddb_json_list = ddb_json_list[batch_size:]

            unprocessed_items = self.batchwrite(batch_items, tableName)

            if len(unprocessed_items) > batch_size // 2:
                batch_size = max(batch_size // 2, 1)

            # Put unprocessed items at back of queue.
            ddb_json_list.extend(unprocessed_items)

    def main(self):
        df = self.getScanIDs()
        df = self.addProcColumns(df)
        ddb_json_list = self.python_to_dynamo(df)
        self.createDDBTable(config.trackingdb_tablename)
        self.write2DDBTable(ddb_json_list, config.trackingdb_tablename)


if __name__ == '__main__':

    # Connect to AWS and create boto3 session
    session = boto3.Session(
        aws_access_key_id=config.aws_access_key_id,
        aws_secret_access_key=config.aws_secret_access_key,
        region_name=config.aws_region
    )
    try:
        pullABCD_IDs(session, config.trackingdb_tablename)
    except Exception as exc:
        raise exc
