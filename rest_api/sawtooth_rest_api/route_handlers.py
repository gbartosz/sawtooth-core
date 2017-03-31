# Copyright 2016 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ------------------------------------------------------------------------------

import json
import base64
from aiohttp import web
# pylint: disable=no-name-in-module,import-error
# needed for the google.protobuf imports to pass pylint
from google.protobuf.json_format import MessageToDict
from google.protobuf.message import DecodeError
from google.protobuf.message import Message as BaseMessage

from sawtooth_sdk.client.exceptions import ValidatorConnectionError
from sawtooth_sdk.client.future import FutureTimeoutError
from sawtooth_sdk.client.stream import Stream
from sawtooth_sdk.protobuf.validator_pb2 import Message

import sawtooth_rest_api.exceptions as errors
import sawtooth_rest_api.error_handlers as error_handlers
from sawtooth_rest_api.protobuf import client_pb2
from sawtooth_rest_api.protobuf.block_pb2 import BlockHeader
from sawtooth_rest_api.protobuf.batch_pb2 import BatchList
from sawtooth_rest_api.protobuf.batch_pb2 import BatchHeader
from sawtooth_rest_api.protobuf.transaction_pb2 import TransactionHeader


DEFAULT_TIMEOUT = 300


class RouteHandler(object):
    """Contains a number of aiohttp handlers for endpoints in the Rest Api.

    Each handler takes an aiohttp Request object, and uses the data in
    that request to send Protobuf message to a validator. The Protobuf response
    is then parsed, and finally an aiohttp Response object is sent back
    to the client with JSON formatted data and metadata.

    If something goes wrong, an aiohttp HTTP exception is raised or returned
    instead.

    Args:
        stream_url (str): The TCP url to communitcate with the validator
        timeout (int, optional): The time in seconds before the Api should
            cancel a request and report that the validator is unavailable.
    """
    def __init__(self, stream_url, timeout=DEFAULT_TIMEOUT):
        self._stream = Stream(stream_url)
        self._timeout = timeout

    async def submit_batches(self, request):
        """Accepts a binary encoded BatchList and submits it to the validator.

        Request:
            body: octet-stream BatchList of one or more Batches
            query:
                - wait: Request should not return until all batches committed

        Response:
            status:
                 - 200: Batches submitted, but wait timed out before committed
                 - 201: All batches submitted and committed
                 - 202: Batches submitted and pending (not told to wait)
            data: Status of uncommitted batches (if any, when told to wait)
            link: /batches or /batch_status link for submitted batches

        """
        # Parse request
        if request.headers['Content-Type'] != 'application/octet-stream':
            return errors.WrongBodyType()

        payload = await request.read()
        if not payload:
            return errors.EmptyProtobuf()

        try:
            batch_list = BatchList()
            batch_list.ParseFromString(payload)
        except DecodeError:
            return errors.BadProtobuf()

        # Query validator
        error_traps = [error_handlers.InvalidBatch()]
        validator_query = client_pb2.ClientBatchSubmitRequest(
            batches=batch_list.batches)
        self._set_wait(request, validator_query)

        response = self._query_validator(
            Message.CLIENT_BATCH_SUBMIT_REQUEST,
            client_pb2.ClientBatchSubmitResponse,
            validator_query,
            error_traps)

        # Build response envelope
        data = response['batch_statuses'] or None
        link = '{}://{}/batch_status?id={}'.format(
            request.scheme,
            request.host,
            ','.join(b.header_signature for b in batch_list.batches))

        if data is None:
            status = 202
        elif any(s != 'COMMITTED' for _, s in data.items()):
            status = 200
        else:
            status = 201
            data = None
            link = link.replace('batch_status', 'batches')

        return self._wrap_response(
            data=data,
            metadata={'link': link},
            status=status)

    async def list_statuses(self, request):
        """Fetches the committed status of batches by either a POST or GET.

        Request:
            body: A JSON array of one or more id strings (if POST)
            query:
                - id: A comma separated list of up to 15 ids (if GET)
                - wait: Request should not return until all batches committed

        Response:
            data: A JSON object, with batch ids as keys, and statuses as values
            link: The /batch_status link queried (if GET)
        """
        error_traps = [error_handlers.StatusesNotReturned()]

        # Parse batch ids from POST body, or query paramaters
        if request.method == 'POST':
            if request.headers['Content-Type'] != 'application/json':
                return errors.BadStatusBody()

            ids = await request.json()

            if not isinstance(ids, list):
                return errors.BadStatusBody()
            if len(ids) == 0:
                return errors.MissingStatusId()
            if not isinstance(ids[0], str):
                return errors.BadStatusBody()

        else:
            try:
                ids = request.url.query['id'].split(',')
            except KeyError:
                return errors.MissingStatusId()

        # Query validator
        validator_query = client_pb2.ClientBatchStatusRequest(batch_ids=ids)
        self._set_wait(request, validator_query)

        response = self._query_validator(
            Message.CLIENT_BATCH_STATUS_REQUEST,
            client_pb2.ClientBatchStatusResponse,
            validator_query,
            error_traps)

        # Send response
        if request.method != 'POST':
            metadata = self._get_metadata(request, response)
        else:
            metadata = None

        return self._wrap_response(
            data=response.get('batch_statuses'),
            metadata=metadata)

    async def list_state(self, request):
        """Fetches list of data leaves, optionally filtered by address prefix.

        Request:
            query:
                - head: The id of the block to use as the head of the chain
                - address: Return leaves whose addresses begin with this prefix

        Response:
            data: An array of leaf objects with address and data keys
            head: The head used for this query (most recent if unspecified)
            link: The link to this exact query, including head block
        """
        head = request.url.query.get('head', None)
        address = request.url.query.get('address', None)

        response = self._query_validator(
            Message.CLIENT_STATE_LIST_REQUEST,
            client_pb2.ClientStateListResponse,
            client_pb2.ClientStateListRequest(head_id=head, address=address))

        return self._wrap_response(
            data=response.get('leaves', []),
            metadata=self._get_metadata(request, response))

    async def fetch_state(self, request):
        """Fetches data from a specific address in the validator's state tree.

        Request:
            query:
                - head: The id of the block to use as the head of the chain
                - address: The 70 character address of the data to be fetched

        Response:
            data: The base64 encoded binary data stored at that address
            head: The head used for this query (most recent if unspecified)
            link: The link to this exact query, including head block
        """
        error_traps = [
            error_handlers.MissingLeaf(),
            error_handlers.BadAddress()]

        address = request.match_info.get('address', '')
        head = request.url.query.get('head', None)

        response = self._query_validator(
            Message.CLIENT_STATE_GET_REQUEST,
            client_pb2.ClientStateGetResponse,
            client_pb2.ClientStateGetRequest(head_id=head, address=address),
            error_traps)

        return self._wrap_response(
            data=response['value'],
            metadata=self._get_metadata(request, response))

    async def list_blocks(self, request):
        """Fetches list of blocks from validator, optionally filtered by id.

        Request:
            query:
                - head: The id of the block to use as the head of the chain
                - id: Comma separated list of block ids to include in results

        Response:
            data: JSON array of fully expanded Block objects
            head: The head used for this query (most recent if unspecified)
            link: The link to this exact query, including head block
        """
        head = request.url.query.get('head', None)
        ids = self._get_filter_ids(request)

        response = self._query_validator(
            Message.CLIENT_BLOCK_LIST_REQUEST,
            client_pb2.ClientBlockListResponse,
            client_pb2.ClientBlockListRequest(head_id=head, block_ids=ids))

        blocks = [self._expand_block(b) for b in response['blocks']]
        return self._wrap_response(
            data=blocks,
            metadata=self._get_metadata(request, response))

    async def fetch_block(self, request):
        """Fetches a specific block from the validator, specified by id.
        Request:
            path:
                - block_id: The 128-character id of the block to be fetched

        Response:
            data: A JSON object with the data from the fully expanded Block
            link: The link to this exact query
        """
        error_traps = [
            error_handlers.MissingBlock(),
            error_handlers.InvalidBlockId()]

        block_id = request.match_info.get('block_id', '')

        response = self._query_validator(
            Message.CLIENT_BLOCK_GET_REQUEST,
            client_pb2.ClientBlockGetResponse,
            client_pb2.ClientBlockGetRequest(block_id=block_id),
            error_traps)

        return self._wrap_response(
            data=self._expand_block(response['block']),
            metadata=self._get_metadata(request, response))

    async def list_batches(self, request):
        """Fetches list of batches from validator, optionally filtered by id.

        Request:
            query:
                - head: The id of the block to use as the head of the chain
                - id: Comma separated list of batch ids to include in results

        Response:
            data: JSON array of fully expanded Batch objects
            head: The head used for this query (most recent if unspecified)
            link: The link to this exact query, including head block
        """
        head = request.url.query.get('head', None)
        ids = self._get_filter_ids(request)

        response = self._query_validator(
            Message.CLIENT_BATCH_LIST_REQUEST,
            client_pb2.ClientBatchListResponse,
            client_pb2.ClientBatchListRequest(head_id=head, batch_ids=ids))

        batches = [self._expand_batch(b) for b in response['batches']]
        return self._wrap_response(
            data=batches,
            metadata=self._get_metadata(request, response))

    async def fetch_batch(self, request):
        """Fetches a specific batch from the validator, specified by id.
        Request:
            path:
                - batch_id: The 128-character id of the block to be fetched

        Response:
            data: A JSON object with the data from the fully expanded Batch
            link: The link to this exact query
        """
        error_traps = [
            error_handlers.MissingBatch(),
            error_handlers.InvalidBatchId()]

        batch_id = request.match_info.get('batch_id', '')

        response = self._query_validator(
            Message.CLIENT_BATCH_GET_REQUEST,
            client_pb2.ClientBatchGetResponse,
            client_pb2.ClientBatchGetRequest(batch_id=batch_id),
            error_traps)

        return self._wrap_response(
            data=self._expand_batch(response['batch']),
            metadata=self._get_metadata(request, response))

    def _query_validator(self, req_type, resp_proto, content, traps=None):
        """Sends a request to the validator and parses the response.
        """
        response = self._try_validator_request(req_type, content)
        return self._try_response_parse(resp_proto, response, traps)

    def _try_validator_request(self, message_type, content):
        """Serializes and sends a Protobuf message to the validator.
        Handles timeout errors as needed.
        """
        if isinstance(content, BaseMessage):
            content = content.SerializeToString()

        future = self._stream.send(message_type=message_type, content=content)

        try:
            response = future.result(timeout=self._timeout)
        except FutureTimeoutError:
            raise errors.ValidatorUnavailable()

        try:
            return response.content
        # Caused by resolving a FutureError on validator disconnect
        except ValidatorConnectionError:
            raise errors.ValidatorUnavailable()

    @classmethod
    def _try_response_parse(cls, proto, response, traps=None):
        """Parses the Protobuf response from the validator.
        Uses "error traps" to send back any HTTP error triggered by a Protobuf
        status, both those common to all handlers, and specified individually.
        """
        parsed = proto()
        parsed.ParseFromString(response)
        traps = traps or []

        try:
            traps.append(error_handlers.Unknown(proto.INTERNAL_ERROR))
        except AttributeError:
            # Not every protobuf has every status enum, so pass AttributeErrors
            pass
        try:
            traps.append(error_handlers.NotReady(proto.NOT_READY))
        except AttributeError:
            pass
        try:
            traps.append(error_handlers.MissingHead(proto.NO_ROOT))
        except AttributeError:
            pass

        for trap in traps:
            trap.check(parsed.status)

        return cls.message_to_dict(parsed)

    @staticmethod
    def _wrap_response(data=None, metadata=None, status=200):
        """Creates the JSON response envelope to be sent back to the client.
        """
        envelope = metadata or {}

        if data is not None:
            envelope['data'] = data

        return web.Response(
            status=status,
            content_type='application/json',
            text=json.dumps(
                envelope,
                indent=2,
                separators=(',', ': '),
                sort_keys=True))

    @staticmethod
    def _get_metadata(request, response):
        """Parses out the head and link properties based on the HTTP Request
        from the client, and the Protobuf response from the validator.
        """
        head = response.get('head_id', None)
        if not head:
            return {'link': str(request.url)}

        link = '{}://{}{}?head={}'.format(
            request.scheme,
            request.host,
            request.path,
            head)

        queries = request.url.query.items()
        headless = ['{}={}'.format(k, v) for k, v in queries if k != 'head']
        if len(headless) > 0:
            link += '&' + '&'.join(headless)

        return {'head': head, 'link': link}

    @classmethod
    def _expand_block(cls, block):
        """Deserializes a Block's header, and the header of its Batches.
        """
        cls._parse_header(BlockHeader, block)
        if 'batches' in block:
            block['batches'] = [cls._expand_batch(b) for b in block['batches']]
        return block

    @classmethod
    def _expand_batch(cls, batch):
        """Deserializes a Batch's header, and the header of its Transactions.
        """
        cls._parse_header(BatchHeader, batch)
        if 'transactions' in batch:
            batch['transactions'] = [
                cls._expand_transaction(t) for t in batch['transactions']]
        return batch

    @classmethod
    def _expand_transaction(cls, transaction):
        """Deserializes a Transaction's header.
        """
        return cls._parse_header(TransactionHeader, transaction)

    @classmethod
    def _parse_header(cls, header_proto, obj):
        """Deserializes a base64 encoded Protobuf header.
        """
        header = header_proto()
        header_bytes = base64.b64decode(obj['header'])
        header.ParseFromString(header_bytes)
        obj['header'] = cls.message_to_dict(header)
        return obj

    def _set_wait(self, request, validator_query):
        """Parses the `wait` query parameter, and sets the corresponding
        `wait_for_commit` and `timeout` properties in the validator query.
        """
        wait = request.url.query.get('wait', 'false')
        if wait.lower() != 'false':
            validator_query.wait_for_commit = True
            try:
                validator_query.timeout = int(wait)
            except ValueError:
                # By default, waits for 95% of REST API's configured timeout
                validator_query.timeout = int(self._timeout * 0.95)

    @staticmethod
    def _get_filter_ids(request):
        """Parses the `id` filter paramter from the url query.
        """
        filter_ids = request.url.query.get('id', None)
        return filter_ids and filter_ids.split(',')

    @staticmethod
    def message_to_dict(message):
        """Converts a Protobuf object to a python dict with desired settings.
        """
        return MessageToDict(
            message,
            including_default_value_fields=True,
            preserving_proto_field_name=True)
