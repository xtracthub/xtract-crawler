
import os
import sys
import json
import uuid
import time
import boto3
import logging
import threading

from random import randint
from datetime import datetime
from utils.pg_utils import pg_conn

from queue import Queue
from globus_sdk.exc import GlobusAPIError, TransferAPIError, GlobusTimeoutError
from globus_sdk import (TransferClient, AccessTokenAuthorizer, ConfidentialAppAuthClient)

from .groupers import matio_grouper, simple_ext_grouper

from .base import Crawler

max_crawl_threads = 8

overall_logger = logging.getLogger(__name__)
overall_logger.setLevel(logging.DEBUG)

fh = logging.FileHandler(f"crawl_main.log")
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
fh.setFormatter(formatter)
overall_logger.addHandler(fh)

# Discovery -- we don't want to send it to the parent that's writing to console.
#  See hierarchy (and one-line solution) here: https://opensource.com/article/17/9/python-logging
overall_logger.propagate = False

stream_handler = logging.StreamHandler()
stream_handler.setLevel(logging.ERROR)
overall_logger.addHandler(stream_handler)

seg = simple_ext_grouper.SimpleExtensionGrouper('creds')
mappings = seg.get_mappings()
tallies = {"text": 0, "tabular": 0, "images": 0, "compressed": 0, "other": 0}
size_tallies = {"decompressed": 0, "compressed": 0}

class GlobusCrawler(Crawler):

    def __init__(self, eid, path, crawl_id, trans_token, auth_token, grouper_name=None, logging_level='debug', base_url=None):
        Crawler.__init__(self)
        self.path = path
        self.base_url = base_url  # TODO
        self.eid = eid
        self.group_count = 0
        self.transfer_token = trans_token
        self.auth_token = auth_token
        self.conn = pg_conn()
        self.crawl_id = crawl_id

        self.crawl_status = "STARTING"
        self.worker_status_dict = {}
        self.idle_worker_count = 0
        self.max_crawl_threads = max_crawl_threads
        self.families_to_enqueue = Queue()

        self.fam_count = 0

        self.count_groups_crawled = 0
        self.count_files_crawled = 0
        self.count_bytes_crawled = 0
        self.commit_gap = 0.1

        self.active_commits = 0
        self.commit_threads = 10
        self.success_group_commit_count = 0
        self.commit_completed = False

        self.insert_files_queue = Queue()

        self.commit_queue_empty = True  # TODO: switch back to false when committing turned back on.

        self.client = boto3.client('sqs',
                              aws_access_key_id=os.environ["aws_access"],
                              aws_secret_access_key=os.environ["aws_secret"], region_name='us-east-1')
        print(f"Creating queue for crawl_id: {self.crawl_id}")
        queue = self.client.create_queue(QueueName=f"crawl_{str(self.crawl_id)}")

        if queue["ResponseMetadata"]["HTTPStatusCode"] == 200:
            self.queue_url = queue["QueueUrl"]
        else:
            raise ConnectionError("Received non-200 status from SQS!")
        print(queue)

        try:
            self.token_owner = self.get_uid_from_token()
        except:  # TODO: Real auth that's not just printing.
            overall_logger.info("Unable to authenticate user: Invalid Token. Aborting crawl.")

        logging.info("Launching occasional commit thread")

        self.sqs_push_threads = {}
        self.thr_ls = []
        for i in range(0, self.commit_threads):
            thr = threading.Thread(target=self.enqueue_loop, args=(i,))
            self.thr_ls.append(thr)
            thr.start()
            self.sqs_push_threads[i] = True
        print(f"Successfully started {len(self.sqs_push_threads)} SQS push threads!")

    def db_crawl_end(self):
        cur = self.conn.cursor()
        query = f"UPDATE crawls SET status='complete', ended_on='{datetime.utcnow()}' WHERE crawl_id='{self.crawl_id}';"
        cur.execute(query)

        return self.conn.commit()

    def enqueue_loop(self, thr_id):

        while True:
            insertables = []

            # If empty, then we want to return.
            if self.families_to_enqueue.empty():
                # If ingest queue empty, we can demote to "idle"
                if self.crawl_status == "COMMITTING":
                    self.sqs_push_threads[thr_id] = "IDLE"
                    time.sleep(0.25)

                    # NOW if all threads idle, then return!
                    if all(value == "IDLE" for value in self.sqs_push_threads.values()):
                        self.commit_completed = True
                        return 0
                time.sleep(1)
                continue

            self.sqs_push_threads[thr_id] = "ACTIVE"

            # Remove up to n elements from queue, where n is current_batch.
            current_batch = 1
            while not self.families_to_enqueue.empty() and current_batch < 10:
                insertables.append(self.families_to_enqueue.get())
                self.active_commits -= 1
                current_batch += 1

            # print(f"Insertables: {insertables}")

            logging.debug("[COMMIT] Preparing batch commit -- executing!")

            try:
                response = self.client.send_message_batch(QueueUrl=self.queue_url,
                                                          Entries=insertables)
                logging.debug(f"SQS response: {response}")
            except Exception as e:  # TODO: too vague
                print(f"WAS UNABLE TO PROPERLY CONNECT to SQS QUEUE: {e}")

            self.success_group_commit_count += current_batch

    def get_extension(self, filepath):
        """Returns the extension of a filepath.
        Parameter:
        filepath (str): Filepath to get extension of.
        Return:
        extension (str): Extension of filepath.
        """
        filename = filepath.split('/')[-1]
        extension = None

        if '.' in filename:
            extension = filename.split('.')[-1]
        return extension

    def get_uid_from_token(self):
        # Step 1: Get Auth Client with Secrets.
        client_id = os.getenv("GLOBUS_FUNCX_CLIENT")
        secret = os.getenv("GLOBUS_FUNCX_SECRET")

        # Step 2: Transform token and introspect it.
        conf_app_client = ConfidentialAppAuthClient(client_id, secret)
        token = str.replace(str(self.auth_token), 'Bearer ', '')

        time0 = time.time()
        auth_detail = conf_app_client.oauth2_token_introspect(token)
        time1 = time.time()
        overall_logger.info(f"INTROSPECT TIME: {time1-time0}")

        uid = auth_detail['username']

        return uid

    def gen_group_id(self):
        return uuid.uuid4()

    def get_transfer(self):
        transfer_token = self.transfer_token
        authorizer = AccessTokenAuthorizer(transfer_token)
        transfer = TransferClient(authorizer=authorizer)

        # Print out a directory listing from an endpoint
        try:
            transfer.endpoint_autoactivate(self.eid)
        except GlobusAPIError as ex:
            logging.error(ex)
            if ex.http_status == 401:
                sys.exit('Refresh token has expired. '
                         'Please delete refresh-tokens.json and try again.')
            else:
                raise ex
        return transfer

    def launch_crawl_worker(self, transfer, worker_id):

        # Borrowed from here:
        # https://stackoverflow.com/questions/6386698/how-to-write-to-a-file-using-the-logging-python-module
        file_logger = logging.getLogger(str(worker_id))  # TODO: __name__?
        file_logger.setLevel(logging.DEBUG)

        fh = logging.FileHandler(f"cr_worker_{worker_id}-{max_crawl_threads - 1}.log")
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        fh.setFormatter(formatter)
        file_logger.addHandler(fh)
        file_logger.propagate = False

        self.worker_status_dict[worker_id] = "STARTING"

        # TODO: ARG.
        grouper = matio_grouper.MatIOGrouper(logger=file_logger)
        #grouper = simple_ext_grouper.SimpleExtensionGrouper(creds=None)

        # exit()

        while True:
            t_start = time.time()
            all_file_mdata = {}  # Holds all metadata for a given Globus directory.

            # If so, then we want the worker to return.
            if self.to_crawl.empty():
                # This worker sees an empty queue, AND IF NOT ALREADY "IDLE", should become "IDLE"
                if self.worker_status_dict[worker_id] is not "IDLE":
                    file_logger.info(f"Worker ID: {worker_id} demoted to IDLE.")
                    self.worker_status_dict[worker_id] = "IDLE"
                    self.idle_worker_count += 1

                # If to_crawl is empty, we want to check and see if other crawl_workers idle AND not in 'starting state'
                # If all of the workers are idle AND state != 'STARTING'.
                if self.idle_worker_count >= self.max_crawl_threads:
                    file_logger.info(f"Worker ID: {worker_id} is terminating.")
                    return "CRAWL--COMPLETE"  # TODO: Behavior for collapsing a thread w/ no real return val?

                rand_wait = randint(1, 5)
                time.sleep(rand_wait)
                continue

            # OTHERWISE, pluck an item from queue.
            else:
                # Catch the RARE race condition error where queue HAD elements in check, but has since become empty.
                try:
                    cur_dir = self.to_crawl.get()
                    restart_loop = False
                except Exception as e:
                    file_logger.error("Caught the following race condition exception... ignoring...")
                    file_logger.error(e)

                    # Go back to beginning and check queue again.
                    time.sleep(2)
                    continue

            # In the case where we successfully extracted from queue AND worker not "ACTIVE", make it active.
            if self.worker_status_dict[worker_id] is not "ACTIVE":
                self.worker_status_dict[worker_id] = "ACTIVE"
                file_logger.info(f"Worker ID: {worker_id} promoted to ACTIVE.")

            dir_contents = []
            try:
                while True:
                    try:
                        t_gl_ls_start = time.time()
                        file_logger.debug(f"Expanding directory: {cur_dir}")
                        dir_contents = transfer.operation_ls(self.eid, path=cur_dir)
                        t_gl_ls_end = time.time()

                        file_logger.info(f"Total time to do globus_ls: {t_gl_ls_end - t_gl_ls_start}")
                        break

                    except GlobusTimeoutError as e:
                        file_logger.error("Globus Timeout Error -- retrying")

                        logging.info(e)
                        print(e)
                        pass

                    except Exception as e:

                        file_logger.error(str(e))
                        print(e)
                        if '502' in str(e)[0:4]:
                            file_logger.error("Directory too large...")
                            restart_loop = True
                            break

                        logging.error(f"Caught error : {e}")
                        logging.error(f"Offending directory: {cur_dir}")
                        time.sleep(0.25)  # TODO: bring back once we finish benchmarking.

                if restart_loop:
                    continue

                # Step 1. All files have own file metadata.
                f_names = []
                for entry in dir_contents:

                    full_path = os.path.join(cur_dir, entry['name'])
                    if entry['type'] == 'file':

                        full_url = f"{self.base_url}{full_path}"
                        print(f"URL: {full_url}")

                        f_names.append(full_path)
                        extension = self.get_extension(entry["name"])

                        logging.debug(f"Metadata for full path: {entry}")
                        all_file_mdata[full_path] = {"physical": {"size": entry["size"],
                                                                  "extension": extension, "path_type": "globus"}}

                        ### TODO: ALL OF THE FOLLOWING SHOULD BE TAKEN OUT ###
                        dec_mapping = None
                        print(mappings)
                        exit()
                        for mapping in mappings:
                            if extension is None:
                                print(f"Mapping: {extension}")
                                dec_mapping = "other"
                                break
                            extension = extension.lower()
                            print(f"Extension: {extension}")

                            if extension in mapping:
                                dec_mapping = mapping

                        if dec_mapping is None:
                            # print("Mapping: Other")
                            dec_mapping = "other"

                        tallies[dec_mapping] += 1
                        if dec_mapping == "compressed":
                            size_tallies["compressed"] += entry["size"]
                        else:
                            size_tallies["decompressed"] += entry["size"]



                    elif entry["type"] == "dir":
                        self.to_crawl.put(full_path)
                    continue
                        ### TODO: ********************************************** ###



                # TODO: Bring back after UMICH

                #
                # #  We want to process each potential group of files.
                # families = grouper.group(f_names)
                # # families = grouper.gen_families(f_names)  # TODO: need to enable this for globus (not just gdrive/box)
                #
                #
                # # For all families
                # for family in families:
                #     tracked_files = set()
                #     num_file_count = 0
                #     num_bytes_count = 0
                #
                #     groups = family["groups"]
                #
                #     fam_file_metadata = {}
                #
                #     # print(f"ALL FILE MDATA: {all_file_mdata}")
                #     for filename in family["files"]:
                #         # filename = filename.replace("//", "/")
                #         fam_file_metadata[filename] = all_file_mdata[filename]
                #
                #     family["files"] = fam_file_metadata
                #     family["base_url"] = self.base_url
                #     # family["family_id"] = family
                #
                #     # For all groups in the family
                #     # print(f"Len files in family: {len(family['files'])}")
                #     for group in groups:
                #         self.count_groups_crawled += 1
                #         parser = group["parser"]
                #         logging.debug(f"Parser: {parser}")
                #
                #         gr_id = group
                #         file_list = group["files"]
                #
                #         # print(f"Len files in group: {len(file_list)}")
                #
                #         for f in file_list:
                #
                #             if f not in tracked_files:
                #                 tracked_files.add(f)
                #                 num_file_count += 1
                #                 self.count_files_crawled += 1
                #                 num_bytes_count += all_file_mdata[f]["physical"]["size"]
                #                 self.count_bytes_crawled += all_file_mdata[f]["physical"]["size"]
                #
                #         self.active_commits += 1
                #         self.group_count += 1
                #
                #     self.families_to_enqueue.put({"Id": str(self.fam_count), "MessageBody": json.dumps(family)})
                #     self.fam_count += 1

            except TransferAPIError as e:
                file_logger.error("Problem directory {}".format(cur_dir))
                file_logger.error("Transfer client received the following error:")
                file_logger.error(e)
                print(e)
                self.failed_dirs["failed"].append(cur_dir)
                continue

    def crawl(self, transfer):
        dir_name = "./xtract_metadata"
        os.makedirs(dir_name, exist_ok=True)

        t_start = time.time()
        self.failed_dirs = {"failed": []}
        self.failed_groups = {"illegal_char": []}

        self.to_crawl = Queue()
        self.to_crawl.put(self.path)

        cur = self.conn.cursor()
        now_time = datetime.utcnow()
        crawl_update = f"INSERT INTO crawls (crawl_id, started_on) VALUES " \
            f"('{self.crawl_id}', '{now_time}');"
        cur.execute(crawl_update)
        self.conn.commit()

        list_threads = []
        for i in range(self.max_crawl_threads):
            t = threading.Thread(target=self.launch_crawl_worker, args=(transfer, i))
            list_threads.append(t)
            t.start()

        for t in list_threads:
            t.join()

        self.crawl_status = "COMMITTING"

        print("Waiting for commit to end...")
        for t in self.thr_ls:
            t.join()
        print("COMMIT SUCCESSFULLY ENDED!")

        self.crawl_status = "SUCCEEDED"

        t_end = time.time()

        print(f"TOTAL TIME: {t_end-t_start}")
        print(tallies)
        print(size_tallies)

        overall_logger.info(f"\n***FINAL groups processed for crawl_id {self.crawl_id}: {self.group_count}***")
        overall_logger.info(f"\n*** CRAWL COMPLETE  (ID: {self.crawl_id})***")

        while True:
            # TODO 2: Should also not check queue but receive status directly from DB thread.
            if self.commit_queue_empty:
                self.db_crawl_end()
                break
            else:
                print("Crawl completed, but waiting for commit queue to finish!")
                time.sleep(1)

        with open('failed_dirs.json', 'w') as fp:
            json.dump(self.failed_dirs, fp)

        with open('failed_groups.json', 'w') as gp:
            json.dump(self.failed_groups, gp)
