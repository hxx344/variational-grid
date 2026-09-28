"""Offline CL/BZ metadata, HTTP observation time and paper-feed boundaries."""
from copy import deepcopy
from email.utils import formatdate
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from variational_grid.client import Client
from variational_grid.cl_bz_market import CommodityClient, market_gate
from variational_grid.cl_bz_scalper import ScalperConfig, ScalperFeed
from variational_grid.models import D, GridError, Quote, utc


NOW = 1790269000


def metadata(symbol='CL', **changes):
    return {symbol: [{
        'asset': symbol, 'has_perp': True, 'instrument_type': 'perpetual_rwa_future',
        'asset_class': 'commodity', 'market_status': 'open', 'is_close_only_mode': False,
        **changes,
    }]}


def session(opens=NOW - 3600, closes=NOW + 3600):
    return {'open': utc(opens), 'close': utc(closes)}


def market(*, state='open', source=NOW, closes=None):
    return {'state': state, 'source_ts': source, 'closes_at': closes,
            'schedule_known': closes is not None}


class Response(io.BytesIO):
    def __init__(self, data, headers=None):
        super().__init__(json.dumps(data).encode())
        self.headers = {'Date': formatdate(NOW, usegmt=True)} if headers is None else headers


class Opener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def client_fixture(client_type=Client, responses=()):
    opener = Opener(responses)
    client = client_type('fixture-session-never-read', opener=opener)
    # Synthetic, in-memory values: never open session files or use real credentials.
    client.session = lambda: ('fixture.token.signature', 'fixture-agent')
    return client, opener


class ObservationTransportTests(unittest.TestCase):
    def request(self, headers, received=NOW):
        client, opener = client_fixture(responses=[Response({'ok': True}, headers)])
        with patch('variational_grid.client.time.time', return_value=received):
            result = client.request('GET', '/metadata/supported_assets', query={'cex_asset': 'CL'}, with_observation=True)
        self.assertEqual(opener.requests[0].method, 'GET')
        self.assertEqual(urlsplit(opener.requests[0].full_url).path, '/api/metadata/supported_assets')
        return result

    def test_http_date_and_cache_age_use_the_older_source_without_double_counting(self):
        for date_age, cache_age, expected in ((6, '6', 6), (6, '25', 25), (25, '6', 25), (4, None, 4)):
            with self.subTest(date_age=date_age, cache_age=cache_age):
                headers = {'Date': formatdate(NOW - date_age, usegmt=True)}
                if cache_age is not None:
                    headers['Age'] = cache_age
                data, received, source = self.request(headers)
                self.assertEqual(data, {'ok': True})
                self.assertEqual(received, NOW)
                self.assertEqual(received - source, expected)

    def test_missing_date_does_not_invent_a_metadata_source_timestamp(self):
        for headers in ({}, {'Age': '10'}):
            with self.subTest(headers=headers):
                self.assertIsNone(self.request(headers)[2])

    def test_malformed_dates_and_invalid_cache_age_fail_closed(self):
        headers = [{'Date': 'not-a-date'}, {'Date': 'Mon, 28 Sep 2026 00:00:00'}]
        headers += [{'Date': formatdate(NOW, usegmt=True), 'Age': age}
                    for age in ('-1', 'nan', 'inf', 'broken')]
        for values in headers:
            with self.subTest(headers=values), self.assertRaises(GridError):
                self.request(values)

    def test_cache_age_includes_response_latency_before_metadata_ttl_check(self):
        now = [NOW]

        class DelayedResponse(Response):
            def read(self, size=-1):
                now[0] += 3
                return super().read(size)

        response = DelayedResponse(metadata(), {'Date': formatdate(NOW, usegmt=True), 'Age': '119'})
        client, _ = client_fixture(CommodityClient, [response])
        with patch('variational_grid.client.time.time', side_effect=lambda: now[0]):
            with self.assertRaisesRegex(GridError, 'stale|timestamp'):
                client.market_observation('CL')

    def test_missing_or_expired_session_cannot_reach_transport(self):
        for reason in ('Cannot read session', 'Session expired or expiring'):
            with self.subTest(reason=reason):
                client, opener = client_fixture(responses=[])
                with patch.object(client, 'session', side_effect=GridError(reason)):
                    with self.assertRaisesRegex(GridError, reason):
                        client.request('GET', '/metadata/supported_assets', with_observation=True)
                self.assertEqual(opener.requests, [])

    def test_observation_flag_does_not_expand_paper_endpoint_allowlist(self):
        client, opener = client_fixture(responses=[])
        for method, path in (('POST', '/orders/new/market'), ('POST', '/orders/new/limit'),
                             ('DELETE', '/orders'), ('POST', '/quotes/accept'), ('GET', '/positions')):
            with self.subTest(method=method, path=path), self.assertRaisesRegex(GridError, 'not permitted'):
                client.request(method, path, with_observation=True)
        self.assertEqual(opener.requests, [])

    def test_regular_requests_preserve_existing_plain_json_result(self):
        client, _ = client_fixture(responses=[Response({'ok': True}, {})])
        self.assertEqual(client.request('GET', '/metadata/supported_assets'), {'ok': True})


class CommodityMetadataTests(unittest.TestCase):
    def observe(self, data=None, *, now=NOW, headers=None, symbol='CL'):
        client, opener = client_fixture(CommodityClient, [Response(metadata(symbol) if data is None else data, headers)])
        with patch('variational_grid.client.time.time', return_value=now):
            result = client.market_observation(symbol)
        request = opener.requests[0]
        self.assertEqual(request.method, 'GET')
        self.assertEqual(parse_qs(urlsplit(request.full_url).query), {'cex_asset': [symbol]})
        return result

    def test_unknown_schedule_remains_explicit_without_inventing_a_close(self):
        for symbol in ('CL', 'BZ'):
            with self.subTest(symbol=symbol):
                result = self.observe(symbol=symbol)
                self.assertEqual(result, market())
                self.assertEqual(market_gate({'CL': result, 'BZ': result}, NOW), (None, None))

    def test_status_close_only_and_closed_override_available_session(self):
        for status, close_only, expected in (('open', False, 'open'), ('open', True, 'close_only'), ('closed', False, 'closed')):
            with self.subTest(status=status, close_only=close_only):
                result = self.observe(metadata(market_status=status, is_close_only_mode=close_only,
                                               trading_sessions=[session()]))
                self.assertEqual(result['state'], expected)
                self.assertTrue(result['schedule_known'])
                self.assertEqual(result['closes_at'], NOW + 3600)

    def test_trading_sessions_include_open_and_exclude_close_or_midday_gap(self):
        sessions = [session(NOW, NOW + 10), session(NOW + 20, NOW + 30)]
        for offset, expected in ((-1, 'closed'), (0, 'open'), (9, 'open'), (10, 'closed'), (19, 'closed'), (20, 'open'), (30, 'closed')):
            with self.subTest(offset=offset):
                result = self.observe(metadata(trading_sessions=sessions), now=NOW + offset)
                self.assertEqual(result['state'], expected)
                self.assertTrue(result['schedule_known'])

    def test_missing_or_stale_http_observation_is_rejected(self):
        headers = ({}, {'Date': formatdate(NOW - 121, usegmt=True)},
                   {'Date': formatdate(NOW, usegmt=True), 'Age': '121'})
        for values in headers:
            with self.subTest(headers=values), self.assertRaisesRegex(GridError, 'stale|timestamp'):
                self.observe(headers=values)
        self.assertEqual(self.observe(headers={'Date': formatdate(NOW - 120, usegmt=True)})['source_ts'], NOW - 120)

    def test_metadata_schema_changes_and_ambiguous_instruments_are_rejected(self):
        wrong = [[], {}, {'CL': None}, {'CL': [None]}, {'CL': []},
                 {'CL': metadata()['CL'] * 2}, metadata(asset='BZ'), metadata(has_perp=False),
                 metadata(instrument_type='spot'), metadata(asset_class='index'),
                 metadata(market_status='halted'), metadata(is_close_only_mode='false')]
        missing = metadata()
        del missing['CL'][0]['is_close_only_mode']
        wrong.append(missing)
        for index, data in enumerate(wrong):
            with self.subTest(case=index), self.assertRaises(GridError):
                self.observe(data)

    def test_invalid_or_timezone_free_sessions_are_rejected(self):
        for sessions in ([], {}, [session(NOW + 10, NOW)], [{'open': '2026-09-24T00:00:00', 'close': utc(NOW)}],
                         [{'open': utc(NOW)}], [None], [session()] * 1001):
            with self.subTest(sessions=str(sessions)[:80]), self.assertRaises(GridError):
                self.observe(metadata(trading_sessions=sessions))


class FeedClient:
    def __init__(self, observations=None, *, now=lambda: NOW, quote_error=False, after_quote=None):
        self.observations = observations or {'CL': market(), 'BZ': market()}
        self.now = now
        self.quote_error = quote_error
        self.after_quote = after_quote
        self.calls = []

    def market_observation(self, symbol):
        self.calls.append(('metadata', symbol))
        row = self.observations[symbol]
        if isinstance(row, Exception):
            raise row
        return deepcopy(row)

    def quote(self, symbol, quantity):
        self.calls.append(('indicative', symbol, str(quantity)))
        if self.quote_error:
            raise GridError('Fixture RFQ unavailable')
        if self.after_quote:
            self.after_quote()
        mark = D('70') if symbol == 'CL' else D('75')
        return Quote(symbol, mark - D('.01'), mark + D('.01'), mark, quantity, self.now())

    def __getattr__(self, name):
        raise AssertionError(f'Feed must not call trading or unrelated client method: {name}')


class ScalperFeedTests(unittest.TestCase):
    def setUp(self):
        self.experiment = SimpleNamespace(settings=ScalperConfig(state_file='unused.sqlite3').validate())

    def next(self, client):
        with patch('variational_grid.cl_bz_scalper.time.time', side_effect=client.now):
            return ScalperFeed(self.experiment, client=client).next()

    def test_feed_uses_only_metadata_and_quantity_specific_indicative_quotes(self):
        client = FeedClient()
        frame = self.next(client)
        self.assertIsNone(frame.reason)
        self.assertEqual(set(frame.quotes), {'CL', 'BZ'})
        self.assertEqual([frame.quotes[s]['qty'] for s in ('CL', 'BZ')], ['1', '1'])
        self.assertCountEqual(client.calls, [('metadata', 'CL'), ('metadata', 'BZ'),
                                            ('indicative', 'CL', '1'), ('indicative', 'BZ', '1')])

    def test_metadata_failure_pauses_and_preserves_known_close_barrier(self):
        client = FeedClient({'CL': GridError('Fixture metadata failure'), 'BZ': market()})
        feed = ScalperFeed(self.experiment, client=client)
        feed.markets = {'CL': market(closes=NOW + 180), 'BZ': market()}
        with patch('variational_grid.cl_bz_scalper.time.time', return_value=NOW):
            frame = feed.next()
        self.assertEqual(frame.markets['CL']['state'], 'unknown')
        self.assertEqual(frame.markets['CL']['closes_at'], NOW + 180)
        self.assertTrue(frame.reason)
        self.assertEqual(frame.quotes, {})
        self.assertTrue(all(call[0] == 'metadata' for call in client.calls))

    def test_either_leg_closed_close_only_unknown_stale_or_closing_stops_rfqs(self):
        for symbol in ('CL', 'BZ'):
            for changed in (market(state='closed'), market(state='close_only'), market(state='unknown'),
                            market(source=NOW - 121), market(source=None), market(closes=NOW + 300)):
                with self.subTest(symbol=symbol, changed=changed):
                    observations = {'CL': market(), 'BZ': market()}
                    observations[symbol] = changed
                    client = FeedClient(observations)
                    frame = self.next(client)
                    self.assertTrue(frame.reason)
                    self.assertEqual(frame.quotes, {})
                    self.assertTrue(all(call[0] == 'metadata' for call in client.calls))

    def test_quote_failure_yields_paused_frame_without_partial_pair(self):
        frame = self.next(FeedClient(quote_error=True))
        self.assertTrue(frame.reason)
        self.assertEqual(frame.quotes, {})

    def test_rfq_crossing_into_close_lead_time_or_close_is_discarded(self):
        for advance in (2, 302):
            with self.subTest(advance=advance):
                now = [NOW]
                client = FeedClient({'CL': market(closes=NOW + 301), 'BZ': market()},
                                    now=lambda: now[0], after_quote=lambda: now.__setitem__(0, NOW + advance))
                frame = self.next(client)
                self.assertTrue(frame.reason, 'Market gate must be checked again after both RFQs return')
                self.assertEqual(frame.quotes, {}, 'Cross-boundary observations cannot feed simulated orders')


if __name__ == '__main__':
    unittest.main()
