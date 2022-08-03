import datetime
from distutils.log import debug
import dateutil.parser
import pytz

from tap_tester import runner, menagerie, connections

from base import IntercomBaseTest


class IntercomBookmarks(IntercomBaseTest):
    @staticmethod
    def name():
        return "tap_tester_intercom_bookmarks"

    @staticmethod
    def convert_state_to_utc(date_str):
        """
        Convert a saved bookmark value of the form '2020-08-25T13:17:36-07:00' to
        a string formatted utc datetime,
        in order to compare against json formatted datetime values
        """
        date_object = dateutil.parser.parse(date_str)
        date_object_utc = date_object.astimezone(tz=pytz.UTC)
        return datetime.datetime.strftime(date_object_utc, "%Y-%m-%dT%H:%M:%SZ")

    def calculated_states_by_stream(self, current_state):
        """
        Look at the bookmarks from a previous sync and set a new bookmark
        value based off timedelta expectations. This ensures the subsequent sync will replicate
        at least 1 record but, fewer records than the previous sync.

        If the test data is changed in the future this will break expectations for this test.

        The following streams barely make the cut:

        companies           "2021-06-14T00:00:00.000000Z"
                            "2021-06-15T00:00:00.000000Z"
        company_segments    "2021-06-14T00:00:00.000000Z"
                            "2021-06-15T00:00:00.000000Z"
        conversations       '2021-06-14T00:00:00.000000Z'
                            '2021-06-15T00:00:00.000000Z'
        contacts            '2021-06-14T00:00:00.000000Z'
                            '2021-06-15T00:00:00.000000Z'
        segments            '2021-06-14T00:00:00.000000Z'
                            '2021-06-15T00:00:00.000000Z'
        """
        timedelta_by_stream = {stream: [30,0,0]  # {stream_name: [days, hours, minutes], ...}
                               for stream in self.expected_streams()}

        stream_to_calculated_state = {stream: "" for stream in current_state['bookmarks'].keys()}
        for stream, state in current_state['bookmarks'].items():
            state_key, state_value = next(iter(state.keys())), next(iter(state.values()))            
            state_as_datetime = dateutil.parser.parse(state_value)

            days, hours, minutes = timedelta_by_stream[stream]
            calculated_state_as_datetime = state_as_datetime - datetime.timedelta(days=days, hours=hours, minutes=minutes)

            state_format = '%Y-%m-%dT%H:%M:%S-00:00'
            calculated_state_formatted = datetime.datetime.strftime(calculated_state_as_datetime, state_format)

            stream_to_calculated_state[stream] = {state_key: calculated_state_formatted}

        return stream_to_calculated_state


    def test_run(self):
        # Created card for untestable/unstable streams.
        # FIX CARD: https://jira.talendforge.org/browse/TDL-17035
        untestable_streams = {"companies", "company_segments", "conversation_parts", "conversations"}
        # Contacts stream does 3 API calls for addressable list fields, [notes, companies, tags]
        # This cause the build to run more than 3 hrs, thus skipping this stream
        stream_to_skip = {"contacts"}
        expected_streams =  self.expected_streams() - untestable_streams - stream_to_skip
    
        expected_replication_keys = self.expected_replication_keys()
        expected_replication_methods = self.expected_replication_method()

        self.start_date_1 = '2021-01-01T00:00:00Z'

        ##########################################################################
        ### First Sync
        ##########################################################################

        self.start_date = self.start_date_1
        conn_id = connections.ensure_connection(self, original_properties=False)

        # Run in check mode
        found_catalogs = self.run_and_verify_check_mode(conn_id)

        # Select only the expected streams tables
        catalog_entries = [ce for ce in found_catalogs if ce['tap_stream_id'] in expected_streams]
        self.perform_and_verify_table_and_field_selection(conn_id, catalog_entries, select_all_fields=True)

        # Run a sync job using orchestrator
        first_sync_record_count = self.run_and_verify_sync(conn_id)
        first_sync_records = runner.get_records_from_target_output()
        first_sync_bookmarks = menagerie.get_state(conn_id)

        ##########################################################################
        ### Update State Between Syncs
        ##########################################################################

        new_states = {'bookmarks': dict()}
        simulated_states = self.calculated_states_by_stream(first_sync_bookmarks)
        for stream, new_state in simulated_states.items():
            new_states['bookmarks'][stream] = new_state
        menagerie.set_state(conn_id, new_states)

        ##########################################################################
        ### Second Sync
        ##########################################################################

        second_sync_record_count = self.run_and_verify_sync(conn_id)
        second_sync_records = runner.get_records_from_target_output()
        second_sync_bookmarks = menagerie.get_state(conn_id)

        ##########################################################################
        ### Test By Stream
        ##########################################################################

        for stream in expected_streams:
            with self.subTest(stream=stream):

                # expected values
                expected_replication_method = expected_replication_methods[stream]

                # collect information for assertions from syncs 1 & 2 base on expected values
                first_sync_count = first_sync_record_count.get(stream, 0)
                second_sync_count = second_sync_record_count.get(stream, 0)
                first_sync_messages = [record.get('data') for record in
                                       first_sync_records.get(stream, {}).get('messages',{})
                                       if record.get('action') == 'upsert']
                second_sync_messages = [record.get('data') for record in
                                        second_sync_records.get(stream, {}).get('messages',{})
                                        if record.get('action') == 'upsert']
                first_bookmark_key_value = first_sync_bookmarks.get('bookmarks', {stream: None}).get(stream)
                second_bookmark_key_value = second_sync_bookmarks.get('bookmarks', {stream: None}).get(stream)


                if expected_replication_method == self.INCREMENTAL:


                    # collect information specific to incremental streams from syncs 1 & 2
                    replication_key = next(iter(expected_replication_keys[stream]))
                    first_bookmark_value = first_bookmark_key_value.get(replication_key)
                    second_bookmark_value = second_bookmark_key_value.get(replication_key)
                    first_bookmark_value_utc = self.convert_state_to_utc(first_bookmark_value)
                    second_bookmark_value_utc = self.convert_state_to_utc(second_bookmark_value)
                    simulated_bookmark_value = self.convert_state_to_utc(new_states['bookmarks'][stream][replication_key])
                    start_date = self.start_date_1

                    # Verify the first sync sets a bookmark of the expected form
                    self.assertIsNotNone(first_bookmark_key_value)
                    self.assertIsNotNone(first_bookmark_key_value.get(replication_key))

                    # Verify the second sync sets a bookmark of the expected form
                    self.assertIsNotNone(second_bookmark_key_value)
                    self.assertIsNotNone(second_bookmark_key_value.get(replication_key))

                    # Verify the second sync bookmark is Greater or Equal to the first sync bookmark
                    self.assertGreaterEqual(second_bookmark_value, first_bookmark_value)


                    for record in second_sync_messages:
                        # Verify the second sync bookmark value is the max replication key value for a given stream
                        replication_key_value = record.get(replication_key)
                        self.assertLessEqual(
                            replication_key_value, second_bookmark_value_utc,
                            msg="Second sync bookmark was set incorrectly, a record with a greater replication-key value was synced."
                        )

                        # Verify the second sync replication key value is Greater or Equal to the set bookmark
                        self.assertGreaterEqual(
                            replication_key_value,
                            simulated_bookmark_value,
                            msg="Second sync records do not respect the previous bookmark.")

                    for record in first_sync_messages:
                        # Verify the first sync replication key value is Greater or Equal to the start date
                        replication_key_value = record.get(replication_key)
                        self.assertGreaterEqual(replication_key_value, start_date,
                                                msg="Second sync records do not respect the previous bookmark.")

                        # Verify the first sync bookmark value is the max replication key value for a given stream
                        self.assertLessEqual(
                            replication_key_value, first_bookmark_value_utc,
                            msg="First sync bookmark was set incorrectly, a record with a greater replication-key value was synced."
                        )

                    # Verify the number of records in the 2nd sync is less then the first
                    self.assertLess(second_sync_count, first_sync_count)

                elif expected_replication_method == self.FULL_TABLE:

                    # Verify the syncs do not set a bookmark for full table streams
                    self.assertIsNone(first_bookmark_key_value)
                    self.assertIsNone(second_bookmark_key_value)

                    # Verify the number of records in the second sync is the same as the first
                    self.assertEqual(second_sync_count, first_sync_count)

                else:

                    raise NotImplementedError(
                        "INVALID EXPECTATIONS\t\tSTREAM: {} REPLICATION_METHOD: {}".format(stream, expected_replication_method)
                    )


                # Verify at least 1 record was replicated in the second sync
                self.assertGreater(second_sync_count, 0, msg="We are not fully testing bookmarking for {}".format(stream))
