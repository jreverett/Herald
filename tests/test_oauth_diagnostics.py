"""Dummy-only diagnostics regressions; no real configuration or credentials."""
import json,sys,time,unittest,urllib.error
from pathlib import Path
repo=Path(__file__).resolve().parents[1]
if (repo/'herald_oauth.py').exists():
    sys.path.insert(0,str(repo));sys.path.insert(0,str(repo/'tests'))
import herald_oauth as oauth
import test_oauth as fixtures

class DenialDiagnostics(unittest.TestCase):
    def setUp(self):fixtures.ResourceTests.setUp(self)

    def test_expiry_scope_and_identity_are_distinct_without_claim_values(self):
        cases=[({'exp':int(time.time())-1,'iat':int(time.time())-60},'token_expired'),
               ({'scope':'herald:read'},'insufficient_scope'),
               ({'aud':'private-audience-never-log'},'audience_mismatch'),
               ({'iss':'https://private-issuer-never-log.example/'},'issuer_mismatch'),
               ({'sub':'private-subject-never-log'},'subject_mismatch')]
        tokens=[]
        for changes,reason in cases:
            token='Bearer '+self.keys.token(self.config,changes);tokens.append(token)
            with self.assertRaises(oauth.AuthError):
                self.auth.authenticate(token,'herald:events',rpc_method='events/subscribe')
            self.assertEqual(self.auth.stats()['authentication_denial_reasons_by_method']['events/subscribe'][reason],1)
        stats=json.dumps(self.auth.stats())
        for sensitive in tokens+['private-audience-never-log','private-issuer-never-log','private-subject-never-log','dummy|jamie']:
            self.assertNotIn(sensitive,stats)
        self.assertEqual(self.auth.stats()['authentication_denials'],len(cases))

    def test_unknown_method_is_bounded_and_header_absence_identifiable(self):
        with self.assertRaises(oauth.AuthError):
            self.auth.authenticate(None,'herald:events',rpc_method='private-method-never-log')
        diagnostics=self.auth.stats()['authentication_denial_reasons_by_method']
        self.assertEqual(diagnostics['other']['missing_authorization'],1)
        self.assertNotIn('private-method-never-log',json.dumps(diagnostics))
        self.assertEqual(set(diagnostics),{'tools/call','events/list','events/subscribe','events/unsubscribe','other'})

class HTTPDiagnostics(unittest.TestCase):
    def setUp(self):fixtures.OAuthHTTPTests.setUp(self)

    def test_event_access_check_requests_scope_without_mailbox_or_event_effects(self):
        tools=self.f.rpc('jamie','tools/list')['result']['tools']
        tool=next(t for t in tools if t['name']=='check_event_access')
        self.assertEqual(tool['securitySchemes'],[{'type':'oauth2','scopes':['herald:events']}])
        self.assertTrue(tool['annotations']['readOnlyHint'])
        token=self.keys.token(self.configs['jamie'],{'scope':'herald:read'})
        denied=self.f.rpc('jamie','tools/call',{'name':'check_event_access','arguments':{}},token=token)
        self.assertTrue(denied['result']['isError'])
        challenge=denied['result']['_meta']['mcp/www_authenticate'][0]
        self.assertIn('insufficient_scope',challenge)
        self.assertIn('scope="herald:events"',challenge)
        access=self.f.tool('jamie','check_event_access')
        self.assertEqual((access['authorized'],access['message_handling']['approved_peers']),(True,['simon']))
        bridge=self.f.bridges['jamie']
        self.assertEqual(bridge.counts['inbox_scans'],0)
        self.assertEqual(bridge.counts['send_calls'],0)
        self.assertEqual(bridge.counts['reply_calls'],0)
        self.assertEqual(bridge.counts['events_created'],0)
        self.assertEqual(bridge.counts['callback_verifications'],0)
        self.assertEqual(bridge.db.execute('SELECT count(*) FROM subscriptions').fetchone()[0],0)
        self.assertEqual(self.auths['jamie'].stats()['authentication_denial_reasons_by_method']['tools/call']['insufficient_scope'],1)

    def test_event_failure_records_rpc_method_and_tools_still_work(self):
        token=self.keys.token(self.configs['jamie'],{'scope':'herald:read'})
        stats=self.f.rpc('jamie','tools/call',{'name':'usage_stats','arguments':{}},token=token)
        self.assertFalse(stats['result'].get('isError',False))
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.f.rpc('jamie','events/subscribe',{},token=token)
        self.assertEqual(error.exception.code,403)
        diag=self.auths['jamie'].stats()['authentication_denial_reasons_by_method']
        self.assertEqual(diag['events/subscribe']['insufficient_scope'],1)
        self.assertEqual(sum(diag['tools/call'].values()),0)
        self.assertEqual(self.f.bridges['jamie'].counts['callback_verifications'],0)

if __name__=='__main__':unittest.main()
