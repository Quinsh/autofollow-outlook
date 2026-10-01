"""Local dashboard security and command integration; no Gmail calls."""
from contextlib import closing
import http.client
import io
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from autofollow.config import DEFAULT_CONFIG_TEXT, initialize_project
from autofollow_ui import App, Handler, ThreadingHTTPServer, build_command, HTML


class UICommandTests(unittest.TestCase):
    def test_commands_are_allowlisted(self):
        for command, options in [('exec', {}), ('send', {'body_file': '/etc/passwd'}), ('init', {'overwrite': True})]:
            with self.subTest(command=command):
                with self.assertRaises(ValueError):
                    build_command(Path('config.toml'), command, options)

    def test_send_values_cannot_become_flags(self):
        args = build_command(Path('config.toml'), 'send', {'to': 'a@example.com', 'subject': '--body-file=/etc/passwd', 'body': '$(touch /tmp/never)', 'dry_run': True})
        self.assertIn('--subject=--body-file=/etc/passwd', args)
        self.assertIn('--body=$(touch /tmp/never)', args)
        self.assertIn('--dry-run', args)
        self.assertNotIn('--body-file', args)

    def test_custom_followup_plans_are_validated_and_serialized(self):
        base = {'to': 'a@example.com', 'subject': 'Hi', 'body': 'Hello'}
        plan = [{'day': 3, 'body': 'Hello Alex\n--literal'}, {'day': 7, 'body': 'Checking once more ✓'},
                {'day': 14, 'body': 'One last message'}]
        for value in [[], plan[:1], plan[:2], plan]:
            args = build_command(Path('config.toml'), 'send', {**base, 'followup_plan': value})
            encoded = next(arg for arg in args if arg.startswith('--followup-plan='))
            self.assertEqual(json.loads(encoded.split('=', 1)[1]), value)
            self.assertFalse(any(arg.startswith(('--max-followups=', '--followup-days=')) for arg in args))
        invalid = [[{'day': 3, 'body': 'Hi [name]'}], [{'day': 3, 'body': 'Hi {name}'}], [{'day': 3, 'body': 'Hi {{name}}'}], None, '[]', [{'day': True, 'body': 'Hi'}], [{'day': 0, 'body': 'Hi'}],
                   [{'day': 1, 'body': ' '}], [{'day': 2, 'body': 'Hi'}, {'day': 1, 'body': 'Hi'}],
                   [{'day': 1, 'body': 'Hi'}, {'day': 1, 'body': 'Hi'}],
                   plan[:2] + [{'day': 7, 'body': 'Third'}], plan + [{'day': 21, 'body': 'Fourth'}]]
        for value in invalid:
            with self.subTest(plan=value), self.assertRaises(ValueError):
                build_command(Path('config.toml'), 'send', {**base, 'followup_plan': value})
        for legacy in ['followup_days', 'max_followups']:
            with self.assertRaises(ValueError):
                build_command(Path('config.toml'), 'send', {**base, 'followup_plan': [], legacy: 1})

    def test_priority_and_reclassify_flags(self):
        self.assertIn('--reclassify', build_command(Path('config.toml'), 'label', {'reclassify': True}))
        self.assertIn('--very-important', build_command(Path('config.toml'), 'showemail', {'very_important': True}))
        for command, key in [('label', 'reclassify'), ('showemail', 'very_important')]:
            with self.assertRaises(ValueError):
                build_command(Path('config.toml'), command, {key: 'true'})

    def test_loop_commands_are_finite(self):
        for command in ['auto', 'run']:
            self.assertIn('--once', build_command(Path('config.toml'), command, {}))

    def test_types_and_required_values(self):
        cases = [('check', {'id': True}), ('check', {'dry_run': 'false'}), ('task', {'id': 1, 'state': 'erase'}), ('task', {'id': 1, 'state': 'snoozed'}), ('send', {'to': 'a@example.com'})]
        for command, options in cases:
            with self.subTest(command=command, options=options):
                with self.assertRaises(ValueError):
                    build_command(Path('config.toml'), command, options)

    def test_task_positionals_and_resolution(self):
        args = build_command(Path('config.toml'), 'task', {'id': 2, 'state': 'done'})
        self.assertEqual(args[-3:], ['task', '2', 'done'])
        args = build_command(Path('config.toml'), 'resolve-send', {'operation_id': 'abc', 'not_delivered': True})
        self.assertEqual(args[-3:], ['resolve-send', 'abc', '--not-delivered'])


class UIAppTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'config.toml'
        self.app = App(self.path)

    def tearDown(self):
        self.app.close()
        self.directory.cleanup()

    def test_snapshot_does_not_initialize_or_authenticate(self):
        snapshot = self.app.snapshot()
        self.assertEqual(snapshot['emails'], [])
        self.assertFalse(snapshot['config']['initialized'])
        self.assertFalse(self.path.exists())
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_statistics_are_read_only_before_setup_and_on_legacy_database(self):
        self.assertEqual(self.app.statistics()['totals']['sent'], 0)
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])
        from autofollow.db import connect, init_db, create_tracked_thread
        cfg = initialize_project(self.path)
        with closing(connect(cfg.database_path)) as con:
            init_db(con)
            create_tracked_thread(con, recipient='a@example.com', recipient_name=None,
                subject='Hi', thread_id='t', initial_message_id='m', initial_rfc_message_id=None,
                initial_sent_at_utc='2026-09-01T12:00:00+00:00', latest_known_message_count=1,
                followup_delay_days=5, max_followups=1, followup_template='Hi')
            con.execute('ALTER TABLE events DROP COLUMN enrollment_message_id')
            con.commit()
        before = cfg.database_path.read_bytes()
        self.assertEqual(self.app.statistics()['totals']['sent'], 1)
        self.assertEqual(cfg.database_path.read_bytes(), before)

    def test_sending_requires_confirmation(self):
        for command in ['send', 'check', 'auto', 'run']:
            options = {'to': 'a@example.com', 'subject': 'Hi', 'body': 'Hello'} if command == 'send' else {}
            with self.subTest(command=command):
                with self.assertRaises(ValueError):
                    self.app.submit(command, options)
        self.assertEqual(self.app.jobs, {})

    def test_classification_double_click_does_not_enqueue_another_run(self):
        with patch.object(self.app.executor, 'submit') as submit:
            job = self.app.submit('label', {'reclassify': True}, request_id='first-request-12345')
            # Retrying the same HTTP request still returns the same job.
            self.assertEqual(self.app.submit('label', {'reclassify': True}, request_id='first-request-12345')['id'], job['id'])
            with self.assertRaisesRegex(ValueError, 'already queued or running'):
                self.app.submit('label', {'reclassify': True}, request_id='second-request-12345')
            self.assertEqual(submit.call_count, 1)

    def test_snapshot_reports_durable_reclassification_counts(self):
        from autofollow.inbox import init_inbox
        from autofollow.db import init_db
        cfg = initialize_project(self.path)
        con = sqlite3.connect(cfg.database_path)
        try:
            init_db(con)
            init_inbox(con)
            con.executemany('INSERT INTO inbox_reclassification VALUES (?,?)',
                            [('a','completed'), ('b','pending'), ('c','unavailable')])
            con.commit()
            self.assertEqual(self.app.snapshot()['reclassification'],
                             dict(total=3, completed=1, pending=1, unavailable=1))
        finally:
            con.close()

    def test_jobs_invoke_subprocess_without_shell(self):
        with patch('autofollow_ui.subprocess.Popen') as run:
            process = run.return_value.__enter__.return_value
            process.stdout = io.StringIO('healthy')
            process.wait.return_value = 0
            job = self.app.submit('doctor', {})
            self.app.executor.shutdown(wait=True)
            result = self.app.jobs[job['id']]
            self.assertEqual(result['status'], 'succeeded')
            self.assertEqual(result['output'], 'healthy')
            self.assertNotIn('shell', run.call_args.kwargs)
            self.assertIsInstance(run.call_args.args[0], list)
            self.assertEqual(run.call_args.args[0][-1], 'doctor')

    def test_retried_send_request_only_queues_once(self):
        options = {'to': 'a@example.com', 'subject': 'Hi', 'body': 'Hello', 'followup_plan': []}
        request_id = 'original-message-123456'
        with patch.object(self.app.executor, 'submit') as submit:
            first = self.app.submit('send', options, confirmed=True, request_id=request_id)
            repeated = self.app.submit('send', dict(reversed(list(options.items()))), confirmed=True, request_id=request_id)
            self.assertEqual(first['id'], repeated['id'])
            self.assertEqual(submit.call_count, 1)
            with self.assertRaisesRegex(ValueError, 'different email'):
                self.app.submit('send', {**options, 'to': 'b@example.com'}, confirmed=True, request_id=request_id)
            del self.app.jobs[first['id']]
            with self.assertRaisesRegex(ValueError, 'already processed'):
                self.app.submit('send', options, confirmed=True, request_id=request_id)
            self.assertEqual(submit.call_count, 1)

    def test_invalid_request_id_never_queues(self):
        with patch.object(self.app.executor, 'submit') as submit:
            for request_id in [[], 1, 'short', 'x' * 129, 'x' * 16 + '\n']:
                with self.subTest(request_id=request_id), self.assertRaises(ValueError):
                    self.app.submit('doctor', {}, request_id=request_id)
            submit.assert_not_called()

    def test_settings_validate_before_replace(self):
        cfg = initialize_project(self.path)
        old = self.path.read_text()
        old_template = cfg.default_template_path.read_text()
        with self.assertRaises(ValueError):
            self.app.save_settings({'config': '[inbox]\ntimezone="Invalid/Zone"', 'template': 'Hello {name}'})
        self.assertEqual(self.path.read_text(), old)
        self.assertEqual(cfg.default_template_path.read_text(), old_template)
        with self.assertRaises(ValueError):
            self.app.save_settings({'config': old, 'template': 'Hello {unknown}'})
        self.app.save_settings({'config': old.replace('initial_lookback_days = 30', 'initial_lookback_days = 45'), 'template': 'Hello {name}'})
        self.assertIn('initial_lookback_days = 45', self.path.read_text())
        self.assertEqual(cfg.default_template_path.read_text(), 'Hello {name}')

    def test_settings_reject_dangerous_template_target(self):
        cfg = initialize_project(self.path)
        contents = self.path.read_text().replace('templates/followup.txt', 'config.toml')
        with self.assertRaises(ValueError):
            self.app.save_settings({'config': contents, 'template': 'Hello'})
        self.assertEqual(self.path.read_text(), DEFAULT_CONFIG_TEXT)

    def test_stop_prevents_cycle_waiting_for_lock(self):
        started = threading.Event()
        with patch.object(self.app, 'submit') as submit:
            with self.app.lock:
                self.app.schedule['enabled'] = True
                def cycle():
                    started.set()
                    self.app._schedule_loop()
                worker = threading.Thread(target=cycle)
                worker.start()
                self.assertTrue(started.wait(1))
                self.app.set_schedule({'enabled': False})
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            submit.assert_not_called()

    def test_snapshot_pending_counts_and_send_metadata(self):
        from autofollow.db import connect, init_db, create_labeled_email
        from autofollow.inbox import init_inbox
        from autofollow.sending import initialize
        cfg = initialize_project(self.path)
        with connect(cfg.database_path) as con:
            init_db(con)
            init_inbox(con)
            initialize(con)
            create_labeled_email(con, gmail_message_id='classified', thread_id='t', subject='Subject', sender='a@example.com', received_at_utc='2026-09-15T12:00:00+00:00', snippet='Snippet', needs_action=True, important=True, category='other', deadline=None, reason='Reason', classification_version=2)
            con.executemany("INSERT INTO inbox_queue(message_id,thread_id,discovered_at,status,error) VALUES(?,?,?,?,?)", [('classified','t','2026-09-15','pending',None), ('deleted','t','2026-09-15','unavailable','Gone'), ('pending','t','2026-09-15','pending','Model unavailable')])
            con.execute("INSERT INTO send_jobs(operation_id,rfc_message_id,raw,metadata,state,created_at_utc,error) VALUES('op','rfc','secret MIME','secret metadata','uncertain','2026-09-15','Timeout')")
        snapshot = self.app.snapshot()
        self.assertEqual(snapshot['counts'], {'classified': 1, 'pending': 1, 'queue_errors': 1, 'unavailable': 1, 'outdated': 0})
        send = snapshot['send_jobs'][0]
        self.assertEqual(send['operation_id'], 'op')
        self.assertEqual(send['error'], 'Timeout')
        self.assertNotIn('raw', send)
        self.assertNotIn('metadata', send)

    def test_snapshot_exposes_priorities_and_places_very_important_first(self):
        from autofollow.db import connect, init_db, create_labeled_email
        cfg = initialize_project(self.path)
        with connect(cfg.database_path) as con:
            init_db(con)
            for message, received, important, very in [('personal', '2026-09-01', True, True), ('routine', '2026-09-15', False, False)]:
                create_labeled_email(con, gmail_message_id=message, thread_id='t', subject=message,
                    sender='a@example.com', received_at_utc=received, snippet='Snippet', needs_action=False,
                    important=important, very_important=very, classification_version=2,
                    category='other', deadline=None, reason='Reason')
        snapshot = self.app.snapshot()
        self.assertEqual([e['gmail_message_id'] for e in snapshot['emails']], ['personal', 'routine'])
        self.assertTrue(snapshot['emails'][0]['very_important'])
        self.assertEqual(snapshot['counts']['outdated'], 0)

    def test_snapshot_counts_reclassification_without_changing_legacy_schema(self):
        cfg = initialize_project(self.path)
        cfg.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(cfg.database_path)) as con, con:
            con.execute('CREATE TABLE labeled_emails (id INTEGER PRIMARY KEY, gmail_message_id TEXT)')
            con.execute('CREATE TABLE inbox_queue (message_id TEXT, error TEXT)')
            con.execute("INSERT INTO labeled_emails VALUES(1,'old')")
            con.executemany('INSERT INTO inbox_queue VALUES(?,?)', [('old', 'retry'), ('new', None)])
        snapshot = self.app.snapshot()
        self.assertEqual(snapshot['counts']['outdated'], 1)
        self.assertEqual(snapshot['counts']['pending'], 2)
        self.assertEqual(snapshot['counts']['queue_errors'], 1)
        with closing(sqlite3.connect(cfg.database_path)) as con, con:
            self.assertEqual(len(list(con.execute('PRAGMA table_info(labeled_emails)'))), 2)
            con.execute('ALTER TABLE labeled_emails ADD COLUMN classification_version INTEGER DEFAULT 0')
            con.execute('ALTER TABLE inbox_queue ADD COLUMN reclassify INTEGER DEFAULT 0')
            con.execute('UPDATE labeled_emails SET classification_version=2')
        self.assertEqual(self.app.snapshot()['counts']['pending'], 1)
        self.assertEqual(self.app.snapshot()['counts']['outdated'], 0)
        with closing(sqlite3.connect(cfg.database_path)) as con, con:
            con.execute("UPDATE inbox_queue SET reclassify=1 WHERE message_id='old'")
        self.assertEqual(self.app.snapshot()['counts']['pending'], 2)
        self.assertEqual(self.app.snapshot()['counts']['queue_errors'], 1)

    def test_snapshot_uses_backend_schedule_for_legacy_and_custom_threads(self):
        from autofollow.db import connect, init_db, create_tracked_thread
        cfg = initialize_project(self.path)
        with connect(cfg.database_path) as con:
            init_db(con)
            tracked_id = create_tracked_thread(con, recipient='a@example.com', recipient_name=None,
                subject='Hi', thread_id='t', initial_message_id='m', initial_rfc_message_id=None,
                initial_sent_at_utc='2026-09-01T12:00:00+00:00', latest_known_message_count=1,
                followup_delay_days=5, max_followups=4, followup_template='Hi {name}')
        self.assertEqual(self.app.snapshot()['threads'][0]['next_due_at_utc'], '2026-09-06T12:00:00+00:00')
        with connect(cfg.database_path) as con:
            con.execute("UPDATE tracked_threads SET followup_plan=?, followup_plan_start_count=2, followup_count=3, last_followup_at_utc=? WHERE id=?",
                        (json.dumps([{'day': 3, 'body': 'First'}, {'day': 7, 'body': 'Second'}]), '2026-09-06T12:00:00+00:00', tracked_id))
        self.assertEqual(self.app.snapshot()['threads'][0]['next_due_at_utc'], '2026-09-10T12:00:00+00:00')
        with connect(cfg.database_path) as con:
            con.execute("UPDATE tracked_threads SET followup_count=4, status='maxed'")
        self.assertIsNone(self.app.snapshot()['threads'][0]['next_due_at_utc'])

    def test_snapshot_reads_old_schema_without_migration(self):
        from autofollow.db import connect, init_db, create_tracked_thread
        cfg = initialize_project(self.path)
        with connect(cfg.database_path) as con:
            init_db(con)
            create_tracked_thread(con, recipient='a@example.com', recipient_name=None,
                subject='Hi', thread_id='t', initial_message_id='m', initial_rfc_message_id=None,
                initial_sent_at_utc='2026-09-01T12:00:00+00:00', latest_known_message_count=1,
                followup_delay_days=5, max_followups=1, followup_template='Hi')
            con.execute('ALTER TABLE tracked_threads DROP COLUMN followup_plan')
            con.execute('ALTER TABLE tracked_threads DROP COLUMN followup_plan_start_count')
        self.assertEqual(self.app.snapshot()['threads'][0]['next_due_at_utc'], '2026-09-06T12:00:00+00:00')
        with connect(cfg.database_path) as con:
            self.assertNotIn('followup_plan', {row[1] for row in con.execute('PRAGMA table_info(tracked_threads)')})

    def test_settings_protect_previous_credentials_path(self):
        cfg = initialize_project(self.path)
        cfg.credentials_path.write_text('original credentials')
        text = self.path.read_text().replace('credentials_path = "credentials.json"', 'credentials_path = "new.json"').replace('templates/followup.txt', 'credentials.json')
        with self.assertRaises(ValueError):
            self.app.save_settings({'config': text, 'template': 'Hello'})
        self.assertEqual(cfg.credentials_path.read_text(), 'original credentials')

    def test_schedule_validates_and_stops_without_operations(self):
        with self.assertRaises(ValueError):
            self.app.set_schedule({'enabled': True, 'interval_minutes': 0})
        with self.assertRaises(ValueError):
            self.app.set_schedule({'enabled': True, 'dry_run': False})
        self.assertFalse(self.app.set_schedule({'enabled': False})['enabled'])

    def test_schedule_runs_live_cycles_and_stops(self):
        # Exercise the real queue and scheduler, replacing only subprocess work.
        completed = threading.Event()
        commands = []
        def run(job_id, args):
            commands.append(args)
            with self.app.lock:
                self.app.jobs[job_id].update(status='succeeded', exit_code=0)
            completed.set()
        def wait(seconds):
            self.assertEqual(seconds, 60)
            self.assertTrue(completed.wait(2))
            completed.clear()
            if len(commands) == 2:
                self.app.set_schedule({'enabled': False})
                return True
            return False
        with patch.object(self.app, '_run', side_effect=run), patch.object(self.app.schedule_stop, 'wait', side_effect=wait):
            self.app.set_schedule({'enabled': True, 'interval_minutes': 1, 'dry_run': False, 'confirmed': True})
            self.app.schedule_thread.join(3)
            self.assertFalse(self.app.schedule_thread.is_alive())
        self.assertEqual(len(commands), 2)
        for args in commands:
            self.assertIn('auto', args)
            self.assertIn('--once', args)
            self.assertNotIn('--dry-run', args)
        self.assertFalse(self.app.schedule['enabled'])
        self.assertIsNone(self.app.schedule['next_at'])

    def test_schedule_recovers_from_submission_error(self):
        attempts = []
        def submit(*args, **kwargs):
            attempts.append(args)
            if len(attempts) == 1:
                raise RuntimeError('temporary queue failure')
        def wait(seconds):
            if len(attempts) == 1:
                self.assertEqual(self.app.schedule['last_error'], 'temporary queue failure')
                return False
            return True
        with patch.object(self.app, 'submit', side_effect=submit), patch.object(self.app.schedule_stop, 'wait', side_effect=wait):
            self.app.set_schedule({'enabled': True, 'dry_run': True})
            self.app.schedule_thread.join(2)
            self.assertFalse(self.app.schedule_thread.is_alive())
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[1], ('auto', {'dry_run': True}))
        self.assertIsNone(self.app.schedule['last_error'])
        self.assertFalse(self.app.schedule['enabled'])

    def test_schedule_skips_busy_queue(self):
        self.app.jobs['busy'] = {'status': 'running'}
        with patch.object(self.app, 'submit') as submit, patch.object(self.app.schedule_stop, 'wait', return_value=True):
            self.app.set_schedule({'enabled': True, 'dry_run': True})
            self.app.schedule_thread.join(2)
        submit.assert_not_called()

    def test_rejected_submission_does_not_block_future_cycles(self):
        with patch.object(self.app.executor, 'submit', side_effect=RuntimeError('unavailable')):
            with self.assertRaises(RuntimeError):
                self.app.submit('auto', {'dry_run': True}, request_id='retryable-request-123')
        self.assertEqual(self.app.jobs, {})
        self.assertEqual(self.app.requests, {})


@unittest.skipUnless(shutil.which('node'), 'Node is required for browser logic checks')
class UIPriorityRenderingTests(unittest.TestCase):
    def test_gpt_drafting_preserves_edits_and_requires_a_fresh_preview(self):
        script = HTML.split('<script>', 1)[1].split('</script>', 1)[0]
        functions = '\n'.join(line for line in script.splitlines() if line.startswith(
            ('const MAX_FOLLOWUPS=', 'function composeOptions(', 'function validateComposePlan(',
             'function bindCompose(', 'function updatePreview(')))
        functions += '\nasync function draftFollowup(' + script.split('async function draftFollowup(', 1)[1].split('\nfunction scheduleStatus(', 1)[0]
        checks = r"""
const assert=require('node:assert/strict');
const DEFAULT_SIGNATURE='Best, Kai',view='compose';
let nodes,buttons,pending,requests=[],jobs=0,previewJob=null,previewFingerprint=null,previewGeneration=0;
const state={jobs:[{id:'preview',status:'succeeded',output:'Reviewed'}]};
const toast=()=>{},confirm=()=>true;
function $(id){return nodes[id]||null}
function node(value=''){return {value,disabled:false,dataset:{},listeners:{},error:'',
  setCustomValidity(message){this.error=message},reportValidity(){return !this.error&&!!this.value.trim()},
  addEventListener(type,listener){this.listeners[type]=listener},
  dispatchEvent(event){this.listeners[event.type]?.(event)}}}
global.FormData=class { *[Symbol.iterator](){for(const key of ['to','subject','body','sender_name','name','cc','bcc'])yield [key,$('#'+key).value]} };
async function api(path,payload){requests.push({path,payload});return new Promise((resolve,reject)=>pending={resolve,reject})}
async function job(){jobs++;return {id:'preview'}}
function setup(){
  nodes={};buttons=[];
  nodes['#compose-form']=node();
  nodes['#compose-form'].querySelectorAll=()=>buttons;
  nodes['#compose-form'].reportValidity=()=>true;
  for(const key of ['to','subject','body','sender_name','name','cc','bcc','send-button','compose-preview'])nodes['#'+key]=node(key+' text');
  $('#to').value='alex@example.com';$('#body').value='Hi Alex, can we meet?\nBest, Kai';
  nodes['#followup-count']=node('3');
  for(let i=1;i<=3;i++){
    for(const key of ['step','day','body'])nodes['#followup-'+key+'-'+i]=node(key==='day'?String(i*3):'Existing '+i);
    nodes['#draft-status-'+i]=node();
    const button=node();button.dataset.draftFollowup=String(i);buttons.push(button);
  }
  bindCompose();
  previewJob='preview';previewFingerprint=JSON.stringify(composeOptions());
}
(async()=>{
  setup();
  let task=buttons[2].listeners.click();
  assert.equal($('#compose-form').dataset.drafting,'true');
  assert.equal($('#send-button').disabled,true);
  assert.equal(previewFingerprint,null);
  assert.ok(buttons.every(b=>b.disabled));
  assert.equal(requests.at(-1).path,'/api/followup-draft');
  assert.deepEqual(requests.at(-1).payload.previous_followups,['Existing 1','Existing 2']);
  assert.equal(requests.at(-1).payload.followup_number,3);
  assert.ok(!('bcc' in requests.at(-1).payload));
  assert.ok(!('cc' in requests.at(-1).payload));
  const calls=requests.length;
  await draftFollowup(buttons[0]);
  assert.equal(requests.length,calls);
  await $('#compose-form').listeners.submit({preventDefault(){}});
  await $('#send-button').listeners.click();
  assert.equal(jobs,0);
  pending.resolve({body:'New third draft'});await task;
  assert.equal($('#followup-body-3').value,'New third draft');
  assert.equal($('#followup-body-1').value,'Existing 1');
  assert.equal($('#followup-body-2').value,'Existing 2');
  assert.equal($('#followup-day-3').value,'9');
  assert.equal($('#body').value,'Hi Alex, can we meet?\nBest, Kai');
  assert.equal($('#send-button').disabled,true);
  assert.equal(previewFingerprint,null);
  assert.ok(buttons.every(b=>!b.disabled));
  assert.match($('#draft-status-3').textContent,/Draft ready/);
  await $('#compose-form').listeners.submit({preventDefault(){}});
  assert.equal(jobs,1);
  assert.equal($('#send-button').disabled,false);
  for(const changed of ['body','followup-body-3','followup-count']){
    setup();task=draftFollowup(buttons[2]);
    $('#'+changed).value=changed==='followup-count'?'2':'My newer edit';
    $('#compose-form').dispatchEvent(new Event('input'));
    const retained=$('#followup-body-3').value;
    pending.resolve({body:'Outdated draft'});await task;
    assert.equal($('#followup-body-3').value,retained);
    assert.match($('#draft-status-3').textContent,/Email changed/);
  }
  setup();task=draftFollowup(buttons[0]);
  pending.reject(Error('OpenAI connection failed'));await task;
  assert.equal($('#followup-body-1').value,'Existing 1');
  assert.match($('#draft-status-1').textContent,/connection failed/);
  assert.equal($('#compose-form').dataset.drafting,'false');
  setup();task=draftFollowup(buttons[0]);
  setup(); // Leave Compose and open another email before the response arrives.
  pending.resolve({body:'Wrong email draft'});await task;
  assert.equal($('#followup-body-1').value,'Existing 1');
  setup();$('#body').value=DEFAULT_SIGNATURE;
  const before=requests.length;
  await draftFollowup(buttons[0]);
  assert.equal(requests.length,before);
})().catch(error=>{console.error(error);process.exitCode=1});
"""
        subprocess.run(['node', '-e', functions + checks], check=True, capture_output=True, text=True)

    def test_compose_three_steps_validate_order_and_serialize_selected_messages(self):
        script = HTML.split('<script>', 1)[1].split('</script>', 1)[0]
        functions = '\n'.join(line for line in script.splitlines() if line.startswith(
            ('const MAX_FOLLOWUPS=', 'function composeOptions(', 'function validateComposePlan(')))
        compose = script.split("if(view==='compose'){", 1)[1].split("\nif(view==='digest')", 1)[0]
        # Execute the real Compose rendering branch and form logic with minimal DOM stubs.
        checks = r"""
const assert=require('node:assert/strict');
const DEFAULT_SIGNATURE='Signature',BLANK_EMAIL='\n\n'+DEFAULT_SIGNATURE;
const state={config:{delay_days:5}},esc=s=>s,header=()=>'',panel=()=>'';
let html='',previewJob=null,previewFingerprint=null;
""" + '{' + compose + r"""
assert.match(html,/<option value="3">Three<\/option>/);
assert.match(html,/id="followup-step-3" hidden disabled/);
assert.match(html,/Write your third follow-up/);
const nodes={};
const $=id=>nodes[id]??=( {value:'',setCustomValidity(message){this.error=message}} );
$('#body').value='Original';
for(let i=1;i<=3;i++){
  $('#followup-day-'+i).value=String([3,7,14][i-1]);
  $('#followup-body-'+i).value='Message '+i;
}
global.FormData=class { *[Symbol.iterator](){yield ['body','Original']} };
for(let count=0;count<=3;count++){
  $('#followup-count').value=String(count);
  validateComposePlan();
  const options=composeOptions();
  assert.equal(options.followup_plan.length,count);
  for(let i=1;i<=3;i++){
    assert.equal($('#followup-step-'+i).hidden,i>count);
    assert.equal($('#followup-step-'+i).disabled,i>count);
    assert.equal($('#followup-day-'+i).error,'');
    if(i<=count)assert.deepEqual(options.followup_plan[i-1],{day:[3,7,14][i-1],body:'Message '+i});
  }
}
for(const day of ['6','7']){
  $('#followup-day-3').value=day;
  validateComposePlan();
  assert.match($('#followup-day-3').error,/later than the previous/);
}
$('#followup-day-3').value='14';
$('#followup-body-3').value=BLANK_EMAIL;
validateComposePlan();
assert.match($('#followup-body-3').error,/Write a follow-up message/);
$('#followup-count').value='2';
validateComposePlan();
assert.equal($('#followup-body-3').error,'');
assert.equal($('#followup-step-3').disabled,true);
$('#followup-count').value='3';
$('#followup-body-3').value='Final message';
validateComposePlan();
assert.equal($('#followup-day-3').error,'');
assert.equal($('#followup-body-3').error,'');
"""
        subprocess.run(['node', '-e', functions + checks], check=True, capture_output=True, text=True)

    def test_thread_menu_exposes_reversible_statistics_action(self):
        script = HTML.split('<script>', 1)[1].split('</script>', 1)[0]
        functions = '\n'.join(line for line in script.splitlines() if line.startswith(
            ('function threadActions(', 'function gmail(')))
        checks = r"""
const assert=require('node:assert/strict');
const esc=s=>String(s).replaceAll('"','&quot;');
const t={id:7,initial_message_id:'initial"id',thread_id:'t',status:'waiting'};
let html=threadActions(t);
assert.match(html,/Exclude from statistics/);
assert.match(html,/data-initial-message="initial&quot;id"/);
assert.match(html,/data-excluded="true"/);
assert.match(html,/Preview check/);
assert.match(html,/Close tracking/);
t.statistics_excluded=true;
html=threadActions(t);
assert.match(html,/Restore to statistics/);
assert.match(html,/data-excluded="false"/);
"""
        subprocess.run(['node', '-e', functions + checks], check=True, capture_output=True, text=True)

    def test_statistics_render_rates_empty_groups_and_history_notices(self):
        script = HTML.split('<script>', 1)[1].split('</script>', 1)[0]
        functions = '\n'.join(line for line in script.splitlines() if line.startswith(
            ('function replyRate(', 'function stageLabel(', 'function stageComparison(', 'function stageCard(', 'function statisticsPage(', 'function header(',
             'function panel(', 'function empty(')))
        checks = r"""
const assert=require('node:assert/strict');
const esc=s=>String(s).replaceAll('<','&lt;'),fmt=s=>s;
let statistics=null,statisticsError='';
assert.match(statisticsPage(),/Loading statistics/);
statisticsError='<network error>';
assert.match(statisticsPage(),/&lt;network error>/);
statisticsError='';
statistics={totals:{sent:4,replied:1,no_reply:3,reply_rate:0.25},
groups:[{followups:0,sent:0,replied:0,no_reply:0,reply_rate:null},
{followups:3,sent:4,replied:1,no_reply:3,reply_rate:0.25}],
historically_inferred:1,unattributed_events:2,generated_at_utc:'today'};
let html=statisticsPage();
assert.match(html,/25\.0%/);
assert.match(html,/class="rs-donut" role="img"/);
assert.match(html,/stroke-dasharray="25 75"/);
assert.match(html,/no data yet/);
const group=(followups,sent,replied)=>({followups,sent,replied,no_reply:sent-replied,reply_rate:sent?replied/sent:null});
let groups=[group(0,10,2),group(1,10,3),group(2,10,1),group(3,10,4)];
let change=stageComparison(groups[1],groups);
assert.equal(change.badge,'+10.0 pp');
assert.match(change.text,/50.0% higher/);
change=stageComparison(groups[2],groups);
assert.equal(change.badge,'−20.0 pp');
assert.match(change.text,/decreases from 30.0% to 10.0%/);
assert.match(change.text,/66.7% lower/);
change=stageComparison(groups[3],groups);
assert.equal(change.badge,'+30.0 pp');
assert.equal(change.detail,'vs. 2 follow-ups');
assert.match(change.text,/increases from 10.0% to 40.0%/);
groups=[group(0,10,0),group(1,10,3)];
assert.match(stageComparison(groups[1],groups).text,/cannot be calculated from a 0% baseline/);
groups=[group(0,10,0),group(1,10,0)];
assert.equal(stageComparison(groups[1],groups).trend,'flat');
assert.match(stageComparison(groups[1],groups).text,/unchanged/);
assert.equal(stageComparison(group(2,0,0),groups).trend,'unknown');
assert.equal(stageComparison(group(4,10,2),groups).trend,'unknown');
assert.match(stageCard(group(0,10,10),[]),/stroke-dasharray="100 0"/);
assert.doesNotMatch(stageCard(group(0,10,0),[]),/class="rs-arc"/);
assert.match(html,/3 follow-ups/);
assert.match(html,/<td>—<\/td>/);
assert.match(html,/1 outreach records/);
assert.match(html,/2 events could not be linked/);
statistics.totals={sent:0,replied:0,no_reply:0,reply_rate:null};
assert.match(statisticsPage(),/No tracked sends yet/);
"""
        subprocess.run(['node', '-e', functions + checks], check=True, capture_output=True, text=True)

    def test_schedule_controls_refresh_without_resetting_settings(self):
        script = HTML.split('<script>', 1)[1].split('</script>', 1)[0]
        functions = '\n'.join(line for line in script.splitlines() if line.startswith(
            ('function scheduleStatus(', 'function updateSchedule(')))
        checks = r"""
const assert=require('node:assert/strict');
const nodes={};
function $(id){return nodes[id]??=( {} )}
function fmt(value){return value}
let state={schedule:{enabled:true,dry_run:false,next_at:'tomorrow'},jobs:[{command:'auto',status:'failed'}]};
$('#interval').value=42;
updateSchedule();
assert.match($('#schedule-status').textContent,/sending enabled/);
assert.equal($('#start-schedule').disabled,true);
assert.equal($('#preview-mode').disabled,true);
assert.match($('#schedule-detail').textContent,/tomorrow.*failed.*Activity/);
state.schedule.dry_run=true;
updateSchedule();
assert.match($('#schedule-status').textContent,/no sending/);
state.schedule.enabled=false;
state.schedule.next_at=null;
updateSchedule();
assert.equal($('#schedule-status').textContent,'Paused');
assert.equal($('#start-schedule').disabled,false);
assert.equal($('#interval').value,42);
assert.equal($('#stop-schedule').disabled,true);
"""
        subprocess.run(['node', '-e', functions + checks], check=True, capture_output=True, text=True)

    def test_resume_controls_do_not_reset_the_queue(self):
        script = HTML.split('<script>', 1)[1].split('</script>', 1)[0]
        functions = '\n'.join(line for line in script.splitlines() if line.startswith(
            ('function classificationBusy(', 'function reclassifyButton(', 'function policyNotice(')))
        checks = r"""
const assert=require('node:assert/strict');
let state={jobs:[],counts:{outdated:0},reclassification:{total:275,completed:41,pending:234,unavailable:0}};
assert.match(policyNotice(), /41 of 275 completed/);
assert.match(reclassifyButton(), /data-command="label"/);
assert.doesNotMatch(reclassifyButton(), /data-reclassify/);
state.jobs=[{command:'label',status:'running'}];
assert.match(reclassifyButton(), /disabled/);
state.jobs=[];state.reclassification.pending=0;state.reclassification.completed=275;
assert.match(reclassifyButton(), /Reclassify all again/);
assert.match(policyNotice(), /Run finished/);
"""
        subprocess.run(['node', '-e', functions + checks], check=True, capture_output=True, text=True)

    def test_lost_job_response_retries_the_same_request_id(self):
        script = HTML.split('<script>', 1)[1].split('</script>', 1)[0]
        function = next(line for line in script.splitlines() if line.startswith('async function job('))
        checks = r"""
const assert=require('node:assert/strict');
let calls=[], messages=[], fail=true;
const crypto={randomUUID:()=> 'test-request-123456'};
async function api(path,payload){calls.push(payload);if(fail){fail=false;throw Error('lost response')}return {id:'same-job'}}
function toast(message){messages.push(message)}
async function refresh(){}
(async()=>{
assert.equal((await job('send',{body:'Hello'},true)).id,'same-job');
assert.equal(calls.length,2);
assert.strictEqual(calls[0],calls[1]);
assert.equal(calls[0].request_id,'test-request-123456');
calls=[];api=async(path,payload)=>{calls.push(payload);const e=Error('Invalid recipient');e.status=400;throw e};
assert.equal(await job('send',{},true),null);
assert.equal(calls.length,1);
assert.equal(messages.at(-1),'Invalid recipient');
})().catch(e=>{console.error(e);process.exit(1)});
"""
        subprocess.run(['node', '-e', function + checks], check=True, capture_output=True, text=True)

    def test_priority_filter_sort_and_distinct_badges(self):
        script = HTML.split('<script>', 1)[1].split('</script>', 1)[0]
        functions = '\n'.join(line for line in script.splitlines() if line.startswith(('function priorityMatch(', 'function priorityOrder(', 'function priorityBadge(')))
        checks = r"""
const assert=require('node:assert/strict');
const normal={id:3,important:0,received_at_utc:'2026-09-15'};
const important={id:2,important:1,received_at_utc:'2026-09-14'};
const very={id:1,important:1,very_important:1,received_at_utc:'2026-09-01',task_state:'done'};
const rows=[normal,important,very];
assert.deepEqual(rows.filter(e=>priorityMatch(e,'very')), [very]);
assert.deepEqual(rows.filter(e=>priorityMatch(e,'important')), [important,very]);
assert.deepEqual(rows.filter(e=>priorityMatch(e,'normal')), [normal]);
assert.equal(rows.filter(e=>priorityMatch(e,'')).length,3);
assert.deepEqual(rows.sort(priorityOrder).map(e=>e.id),[1,3,2]);
assert.equal(priorityBadge(very),'<span class="pill very-important">Very important</span>');
assert.equal(priorityBadge(normal),'');
assert.match(priorityBadge(important),/>Important</);
"""
        subprocess.run(['node', '--check'], input=script, text=True, check=True, capture_output=True)
        subprocess.run(['node', '-e', functions + checks], check=True, capture_output=True, text=True)


class UIHTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.app = App(Path(self.directory.name) / 'config.toml')
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.app = self.app
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.app.close()
        self.directory.cleanup()

    def request(self, path='/', method='GET', headers=None, data=None):
        con = http.client.HTTPConnection('127.0.0.1', self.port)
        try:
            con.request(method, path, body=json.dumps(data) if data is not None else None, headers=headers or {})
            response = con.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode()
        finally:
            con.close()

    def test_root_has_token_and_security_headers(self):
        code, headers, body = self.request()
        self.assertEqual(code, 200)
        self.assertIn(self.app.token, body)
        self.assertEqual(headers['X-Frame-Options'], 'DENY')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])

    def test_state_requires_token(self):
        self.assertEqual(self.request('/api/state')[0], 403)
        code, _, body = self.request('/api/state', headers={'X-Autofollow-Token': self.app.token})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['emails'], [])

    def test_statistics_page_and_protected_api(self):
        code, _, body = self.request('/statistics')
        self.assertEqual(code, 200)
        self.assertIn('data-view="statistics"', body)
        self.assertIn(self.app.token, body)
        self.assertEqual(self.request('/api/statistics')[0], 403)
        headers = {'X-Autofollow-Token': self.app.token}
        code, _, body = self.request('/api/statistics', headers=headers)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['totals']['sent'], 0)
        headers['Origin'] = 'https://evil.example'
        self.assertEqual(self.request('/api/statistics', headers=headers)[0], 403)

    def test_statistics_exclusion_endpoint_persists_without_changing_tracking(self):
        from autofollow.db import connect, init_db, create_tracked_thread, get_thread
        cfg = initialize_project(self.app.config_path)
        with closing(connect(cfg.database_path)) as con:
            init_db(con)
            tracked_id = create_tracked_thread(con, recipient='a@example.com', recipient_name=None,
                subject='Test', thread_id='t', initial_message_id='m', initial_rfc_message_id=None,
                initial_sent_at_utc='2026-09-01T12:00:00+00:00', latest_known_message_count=1,
                followup_delay_days=5, max_followups=1, followup_template='Hi')
            before = get_thread(con, tracked_id)
        data = {'tracked_id': tracked_id, 'initial_message_id': 'm', 'excluded': True}
        headers = {'X-Autofollow-Token': self.app.token, 'Content-Type': 'application/json'}
        path = '/api/statistics/exclusion'
        self.assertEqual(self.request(path, 'POST', {'Content-Type': 'application/json'}, data)[0], 403)
        self.assertEqual(self.request(path, 'POST', {**headers, 'Origin': 'https://evil.example'}, data)[0], 403)
        self.assertEqual(self.request(path, 'POST', headers, {**data, 'excluded': 'true'})[0], 400)
        self.assertEqual(self.request(path, 'POST', headers, data)[0], 200)
        self.assertTrue(self.app.snapshot()['threads'][0]['statistics_excluded'])
        self.assertEqual(self.app.statistics()['totals']['sent'], 0)
        with closing(connect(cfg.database_path)) as con:
            self.assertEqual(get_thread(con, tracked_id), before)
            self.assertEqual(con.execute('SELECT count(*) FROM events').fetchone()[0], 1)
        self.assertEqual(self.request(path, 'POST', headers, {**data, 'excluded': False})[0], 200)
        self.assertFalse(self.app.snapshot()['threads'][0]['statistics_excluded'])
        self.assertEqual(self.app.statistics()['totals']['sent'], 1)

    def test_rebinding_and_cross_origin_blocked(self):
        self.assertEqual(self.request(headers={'Host': 'evil.example'})[0], 403)
        headers = {'X-Autofollow-Token': self.app.token, 'Origin': 'https://evil.example'}
        self.assertEqual(self.request('/api/state', headers=headers)[0], 403)

    def test_post_requires_json_and_rejects_unknown_command(self):
        headers = {'X-Autofollow-Token': self.app.token}
        self.assertEqual(self.request('/api/jobs', 'POST', headers, {'command': 'doctor'})[0], 400)
        headers['Content-Type'] = 'application/json'
        self.assertEqual(self.request('/api/jobs', 'POST', headers, {'command': 'sh'})[0], 400)
        self.assertEqual(self.app.jobs, {})

    def test_gpt_draft_endpoint_requires_session_and_uses_configured_model(self):
        self.app.config_path.write_text('[llm]\nprovider="ollama"\nopenai_model="configured-gpt"\n')
        path = '/api/followup-draft'
        headers = {'X-Autofollow-Token': self.app.token, 'Content-Type': 'application/json'}
        data = {'to': 'alex@example.com', 'subject': 'Meeting', 'body': 'Hi Alex, can we meet?\nBest, Kai',
                'days': [3], 'followup_number': 1, 'previous_followups': []}
        with patch('autofollow.drafting.request_response', return_value={
            'status': 'completed', 'output': [{'type': 'message', 'content': [
                {'type': 'output_text', 'text': '{"body":"Hi Alex, checking in about meeting. Best, Kai"}'}]}]
        }) as fetch, patch('autofollow.gmail_client.GmailClient') as gmail:
            self.assertEqual(self.request(path, 'POST', {'Content-Type': 'application/json'}, data)[0], 403)
            self.assertEqual(self.request(path, 'POST', {**headers, 'Origin': 'https://evil.example'}, data)[0], 403)
            fetch.assert_not_called()
            self.assertEqual(self.request(path, 'POST', headers, {**data, 'days': [1, 2, 3, 4]})[0], 400)
            fetch.assert_not_called()
            code, _, body = self.request(path, 'POST', headers, data)
            self.assertEqual(code, 200)
            self.assertIn('checking in', json.loads(body)['body'])
            self.assertEqual(fetch.call_args.args[0]['model'], 'configured-gpt')
            gmail.assert_not_called()
        self.assertEqual(self.app.jobs, {})
        self.assertFalse((self.app.config_path.parent / '.autofollow').exists())

    def test_gpt_draft_concurrency_and_error_leave_app_available(self):
        headers = {'X-Autofollow-Token': self.app.token, 'Content-Type': 'application/json'}
        path = '/api/followup-draft'
        with self.app.draft_lock, patch('autofollow.drafting.draft_followup') as draft:
            code, _, body = self.request(path, 'POST', headers, {})
            self.assertEqual(code, 400)
            self.assertIn('already in progress', body)
            draft.assert_not_called()
            self.assertEqual(self.request('/api/state', headers=headers)[0], 200)
        with patch('autofollow.drafting.draft_followup', side_effect=RuntimeError('OpenAI connection failed')):
            self.assertEqual(self.request(path, 'POST', headers, {})[0], 400)
        with patch('autofollow.drafting.draft_followup', return_value='A fresh draft'):
            self.assertEqual(self.request(path, 'POST', headers, {})[0], 200)


if __name__ == '__main__':
    unittest.main()
