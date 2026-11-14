"""The Stop control of the browser page (docs/manager.md, "Stopping a job"): its pure parts and its state machine.

When node is installed, ``ui/app.js`` runs in a minimal fake DOM that records event handlers, so the tests drive
the same functions the page does: nothing is sent before the confirmation, a viewer's button is disabled, every
refusal has a fixed message, and hostile text in a reason or a name is only ever shown as text. The static checks
need no node.
"""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

from factory import webui

UI = Path(webui.__file__).resolve().parent / "ui"
NODE = shutil.which("node")

SCRIPT = r"""
const CANARY = '<script>alert(1)</script><img src=x onerror=alert(2)>';
class Text { constructor(text) { this.text = String(text); } }
class El {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.className = ''; this.fns = {}; }
  appendChild(child) { this.children.push(child); return child; }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  removeAttribute(name) { delete this.attrs[name]; }
  addEventListener(type, fn) { (this.fns[type] = this.fns[type] || []).push(fn); }
  get textContent() { return this.children.map((c) => (c instanceof Text ? c.text : c.textContent)).join(''); }
  set innerHTML(value) { throw new Error('innerHTML was set'); }
}
global.document = { createElement: (tag) => new El(tag), createTextNode: (text) => new Text(text) };
const app = require(process.argv[1]);
function walk(node, visit) { visit(node); if (node instanceof El) node.children.forEach((c) => walk(c, visit)); }
function find(nodes, test) { const found = []; for (const node of [].concat(nodes)) walk(node, (n) => { if (n instanceof El && test(n)) found.push(n); }); return found; }
function text(nodes) { let out = ''; for (const node of [].concat(nodes)) walk(node, (n) => { if (n instanceof Text) out += n.text; }); return out; }
function fire(el, type) { let prevented = false; for (const fn of el.fns[type] || []) fn({ preventDefault() { prevented = true; } }); return prevented; }
const b64 = (value) => Buffer.from(JSON.stringify(value)).toString('base64url');
const jwt = (claims) => `${b64({ alg: 'RS256' })}.${b64(claims)}.signature`;

const out = {};
out.roles = [
  app.rolesOf(jwt({ roles: ['Factory.Operator'] })), app.rolesOf(jwt({ roles: ['Factory.Viewer', 7] })), app.rolesOf(jwt({ name: 'x' })),
  app.rolesOf('opaque-token-value'), app.rolesOf('a.b.c'), app.rolesOf(`h.${Buffer.from('not json').toString('base64url')}.s`),
  app.rolesOf(`h.${Buffer.from('"text"').toString('base64url')}.s`), app.rolesOf(null), app.rolesOf(42), app.rolesOf('a.b'),
];
out.may = [null, undefined, [], ['Factory.Viewer'], ['Factory.Operator'], ['Factory.Viewer', 'Factory.Operator'], 'Factory.Operator'].map(app.mayStop);
out.path = [app.stopPath('org/name', 'r1'), app.stopPath('org/na me', 'r1'), app.stopPath('org/name', '../x'), app.stopPath('org', 'r1')];
out.body = [
  app.stopBody('fix-a'), app.stopBody('fix-a', ''), app.stopBody('fix-a', '   \n\t '), app.stopBody('fix-a', '  spaced \n out\u001b[0m\u202e '),
  app.stopBody('fix-a', 'x'.repeat(500)).reason.length, app.stopBody('../x', 'r'), app.stopBody(7, 'r'), app.stopBody('fix-a', 7),
];
out.errors = [
  { status: 401 }, { status: 403 }, { status: 404 }, { status: 409, jobStatus: 'pr_ready' }, { status: 409, jobStatus: CANARY }, { status: 409 },
  { status: 400 }, { status: 415 }, { status: 422 }, { status: 503 }, { status: 0 }, null, { status: 500 },
].map(app.stopErrorText);
out.notes = [
  app.stopNote({ state: 'stopping', requested_by: 'alice', requested_at: '2026-11-09T10:00:00Z' }),
  app.stopNote({ state: 'stopped', requested_by: 'alice', requested_at: '2026-11-09T10:00:00Z', stopped_at: '2026-11-09T10:02:00Z', terminated: true, reason: CANARY }),
  app.stopNote({ state: 'stopped', terminated: false }), app.stopNote(null), app.stopNote({}), app.stopNote({ state: 'Evil State' }), app.stopNote('x'),
];

// The state machine, drawn into a list of views, with a request that is recorded and answered as the test says.
async function scenario(options) {
  const shown = []; const sent = [];
  const ctx = {
    job: 'fix-a', serial: 1, canStop: options.canStop !== false,
    show(nodes, kind) { shown.push({ kind, nodes }); },
    send(body) { sent.push(body); return options.fail ? Promise.reject(options.fail) : Promise.resolve(options.answer); },
  };
  const flow = app.stopFlow(ctx);
  return { ctx, flow, shown, sent, last: () => shown[shown.length - 1] };
}
const view = (s) => { const last = s.last(); return { kind: last.kind, text: text(last.nodes), tags: find(last.nodes, () => true).map((n) => n.tag), buttons: find(last.nodes, (n) => n.tag === 'button').map((n) => ({ text: text(n), disabled: 'disabled' in n.attrs, type: n.attrs.type })), inputs: find(last.nodes, (n) => n.tag === 'input').map((n) => n.attrs) }; };

(async () => {
  const flow = {};
  let s = await scenario({ answer: { stop: 'requested', grace_seconds: 120 } });
  flow.idle = view(s);
  flow.confirmWithoutAsk = { result: await s.flow.confirm('x'), sent: s.sent.length, kind: s.last().kind };
  s.flow.ask(); flow.confirmView = view(s); flow.sentAfterAsk = s.sent.length;
  s.flow.cancel(); flow.afterCancel = { kind: s.last().kind, result: await s.flow.confirm('x'), sent: s.sent.length };
  s.flow.ask();
  const first = s.flow.confirm('  spaced \n out '); const second = s.flow.confirm('again');
  flow.inFlight = view(s);
  await first; await second;
  flow.done = view(s); flow.sent = s.sent; flow.afterDone = { again: await s.flow.confirm('third'), sent: s.sent.length };

  s = await scenario({ fail: { status: 403 } });
  s.flow.ask(); await s.flow.confirm('r'); flow.refused = view(s);
  const retry = find(s.last().nodes, (n) => n.tag === 'button')[0]; fire(retry, 'click'); flow.retried = view(s);

  s = await scenario({ fail: { status: 409, jobStatus: 'pr_ready' } });
  s.flow.ask(); await s.flow.confirm(''); flow.conflict = view(s); flow.conflictSent = s.sent;

  s = await scenario({ canStop: false, answer: {} });
  flow.viewer = view(s); s.flow.ask(); flow.viewerAfterAsk = view(s); flow.viewerConfirm = { result: await s.flow.confirm('x'), sent: s.sent.length };

  s = await scenario({ answer: { stop: 'already_requested' } });
  fire(find(s.last().nodes, (n) => n.tag === 'button')[0], 'click');   // the button itself opens the confirmation
  const form = find(s.last().nodes, (n) => n.tag === 'form')[0];
  flow.submitPrevented = fire(form, 'submit');   // the submit handler confirms (an empty field: no reason)
  await Promise.resolve(); await Promise.resolve();
  flow.submit = { sent: s.sent, view: view(s) };
  out.flow = flow;

  // The views that carry the control.
  const calls = []; const extras = { stopControl(repo, run, id) { calls.push([repo, run, id]); const el = new El('div'); el.className = 'stop'; return el; } };
  const row = (id, status, stop) => ({ id, issues: [1], status, phase: 'build', active_worker: true, attempt: 1, age_seconds: 5, phase_age_seconds: 4, cost_usd: 1, pr: null, blockers: 0, waits: [], owner: 'x', next_action: 'n', stop });
  const active = { generated_at: '2026-11-09T10:00:00Z', repos: [{ repo: 'org/name', runs: [{ run: 'r1', supervisor: { state: 'running' }, budget_usd: null, cost_usd: 1, jobs_total: 4, jobs: [
    row('fix-a', 'running', null), row('fix-b', 'queued', null), row('fix-c', 'running', { state: 'stopping', requested_by: 'alice', requested_at: '2026-11-09T10:00:00Z' }),
    row('fix-d', 'stopped', { state: 'stopped', requested_by: CANARY, reason: CANARY, terminated: true }),
  ] }] }] };
  out.activeWith = { text: text(app.renderActive(active, Date.parse('2026-11-09T10:00:30Z'), extras)), calls: calls.slice() };
  calls.length = 0;
  out.activeWithout = { text: text(app.renderActive(active, Date.parse('2026-11-09T10:00:30Z'))), calls: calls.length };
  const detail = { source: 'state', repo: 'org/name', run: 'r1', job: 'fix-a', issues: [1], status: 'running', phase: 'build', attempt: 1, retries: 0, pr: null, cost_usd: 1, age_seconds: 9, phase_seconds: {}, next_action: 'n', waits: [], blockers: 0, phase_events: [], stop: null };
  calls.length = 0;
  const running = app.renderJob(detail, extras);
  out.jobRunning = { calls: calls.slice(), stopRow: text(find(running, (n) => n.tag === 'dt' && text(n) === 'Stop')) };
  calls.length = 0;
  const stopped = app.renderJob({ ...detail, status: 'stopped', stop: { state: 'stopped', requested_by: 'alice', requested_at: '2026-11-09T10:00:00Z', stopped_at: '2026-11-09T10:01:00Z', terminated: false, reason: CANARY } }, extras);
  out.jobStopped = { calls: calls.length, text: text(stopped), tags: [...new Set(find(stopped, () => true).map((n) => n.tag))] };
  const ledger = app.renderJob({ source: 'ledger', repo: 'org/name', run: 'r1', job: 'fix-a', issues: [1], status: 'stopped', phase: 'build', attempt: 1, history: { outcome: 'stopped', purged: true }, timeline: [] }, extras);
  out.jobLedger = { calls: calls.length, stopRow: find(ledger, (n) => n.tag === 'dt' && text(n) === 'Stop').length };
  console.log(JSON.stringify(out));
})();
"""


def run_node():
    done = subprocess.run([NODE, "-e", SCRIPT, str(UI / "app.js")], capture_output=True, text=True, timeout=60, check=False)
    if done.returncode:
        raise AssertionError(done.stderr)
    return json.loads(done.stdout)


class StaticTests(unittest.TestCase):
    def setUp(self):
        self.script = (UI / "app.js").read_text(encoding="utf-8")
        self.style = (UI / "style.css").read_text(encoding="utf-8")

    def test_the_only_request_the_control_can_make_is_the_confirmed_stop_of_one_job(self):
        self.assertEqual(self.script.count("ctx.send("), 1)  # reached only from `confirm`, which needs the open confirmation
        self.assertEqual(self.script.count("api(path, { body })"), 1)
        self.assertEqual(self.script.count("window.fetch("), 2)  # the token still reaches one fetch; the other reads /ui/config.json without any (test_webui)
        self.assertEqual(self.script.count("Authorization"), 1)
        self.assertIn("'Content-Type'", self.script)
        self.assertIn("/stop`", self.script)
        self.assertNotIn("DELETE", self.script)

    def test_a_poll_does_not_redraw_an_open_confirmation(self):
        # only a box in view holds the poll back (test_webui_board: one in a closed repository must not freeze the board)
        self.assertIn("if (stopsBlocking() || focusedWithoutId(results)) {", self.script)
        self.assertIn("for (const box of openStops.values()) if (box.isConnected && !box.closest('details.repo:not([open])')) return true;", self.script)

    def test_the_box_of_the_control_never_shows_a_left_out_part_as_text(self):
        # a view leaves out what it does not show as null; the page's box drops it before replaceChildren (which would
        # insert the text "null")
        mount = self.script[self.script.index("function mountStop("):]
        mount = mount[: mount.index("const extras = ")]
        self.assertIn("box.replaceChildren(...nodes.filter((node) => node !== null && node !== undefined));", mount)

    def test_the_danger_button_and_the_form_have_styles(self):
        for rule in ("button.danger", ".stop-form"):
            self.assertIn(rule, self.style)


@unittest.skipUnless(NODE, "node is not installed; the stop control is then not exercised")
class ControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_node()

    def test_the_roles_of_a_token_are_a_hint_and_only_a_token_that_names_its_roles_can_disable_the_button(self):
        self.assertEqual(self.out["roles"], [["Factory.Operator"], ["Factory.Viewer"], [], None, None, None, None, None, None, None])
        self.assertEqual(self.out["may"], [True, True, False, False, True, True, False])

    def test_the_path_and_the_body_come_from_validated_parts(self):
        self.assertEqual(self.out["path"], ["/repos/org/name/runs/r1/stop", None, None, None])
        body = self.out["body"]
        self.assertEqual(body[:3], [{"job": "fix-a"}, {"job": "fix-a"}, {"job": "fix-a"}])
        self.assertEqual(body[3], {"job": "fix-a", "reason": "spaced out [0m"})
        self.assertEqual(body[4], 200)
        self.assertEqual(body[5:], [None, None, {"job": "fix-a"}])

    def test_every_refusal_has_a_fixed_message_and_response_text_is_never_shown(self):
        errors = self.out["errors"]
        self.assertIn("Sign in required", errors[0])
        self.assertIn("needs the Factory.Operator role", errors[1])
        self.assertIn("does not know this job", errors[2])
        self.assertIn("not running any more (status pr_ready)", errors[3])
        self.assertNotIn("script", errors[4])  # a status that is not a plain word is not shown
        self.assertIn("no supervisor is running its run", errors[5])
        self.assertEqual(errors[6], errors[7])
        self.assertIn("Nothing was stopped", errors[9])
        self.assertEqual(errors[10], errors[11])
        self.assertIn("could not be reached", errors[10])
        self.assertEqual(len({*errors}), 9)

    def test_notes_say_who_when_and_why_as_plain_lines(self):
        notes = self.out["notes"]
        self.assertEqual(notes[0], ["Stop requested by alice at 2026-11-09 10:00Z. The current step may finish, then the worker is terminated."])
        self.assertIn("Stopped by alice: requested 2026-11-09 10:00Z, stopped 2026-11-09 10:02Z (the worker was terminated after the grace period)", notes[1][0])
        self.assertEqual(notes[1][1], "Reason: <script>alert(1)</script><img src=x onerror=alert(2)>")
        self.assertIn("(the step finished by itself)", notes[2][0])
        self.assertEqual(notes[3:], [None, None, None, None])

    def test_nothing_is_sent_before_the_confirmation_and_the_confirmation_sends_once(self):
        flow = self.out["flow"]
        self.assertEqual(flow["idle"]["kind"], "idle")
        self.assertEqual([button["text"] for button in flow["idle"]["buttons"]], ["Stop"])
        self.assertNotIn("form", flow["idle"]["tags"])
        self.assertEqual(flow["confirmWithoutAsk"], {"result": None, "sent": 0, "kind": "idle"})
        confirm = flow["confirmView"]
        self.assertEqual(confirm["kind"], "confirm")
        self.assertEqual([button["text"] for button in confirm["buttons"]], ["Confirm stop", "Cancel"])
        self.assertEqual(confirm["buttons"][0]["type"], "submit")
        self.assertEqual(confirm["inputs"][0]["maxlength"], "200")
        self.assertIn("left alone", confirm["text"])
        self.assertIn("other jobs of the run go on", confirm["text"])
        self.assertEqual(flow["sentAfterAsk"], 0)
        self.assertEqual(flow["afterCancel"], {"kind": "idle", "result": None, "sent": 0})
        self.assertEqual(flow["inFlight"]["kind"], "sending")
        self.assertEqual(flow["sent"], [{"job": "fix-a", "reason": "spaced out"}])  # two confirmations, one request
        self.assertEqual(flow["done"]["kind"], "done")
        self.assertIn("Stop requested for fix-a.", flow["done"]["text"])
        self.assertIn("within 120 s", flow["done"]["text"])
        self.assertEqual(flow["afterDone"], {"again": None, "sent": 1})

    def test_a_refusal_is_shown_and_the_person_can_try_again_through_the_confirmation(self):
        flow = self.out["flow"]
        self.assertEqual(flow["refused"]["kind"], "error")
        self.assertIn("needs the Factory.Operator role", flow["refused"]["text"])
        self.assertEqual([button["text"] for button in flow["refused"]["buttons"]], ["Try again"])
        self.assertEqual(flow["retried"]["kind"], "confirm")
        self.assertIn("not running any more (status pr_ready)", flow["conflict"]["text"])
        self.assertEqual(flow["conflictSent"], [{"job": "fix-a"}])

    def test_a_viewers_button_is_disabled_says_why_and_cannot_open_the_confirmation(self):
        flow = self.out["flow"]
        self.assertEqual(flow["viewer"]["buttons"], [{"text": "Stop", "disabled": True, "type": "button"}])
        self.assertIn("Stopping needs the Factory.Operator role.", flow["viewer"]["text"])
        self.assertEqual(flow["viewerAfterAsk"]["kind"], "idle")
        self.assertEqual(flow["viewerConfirm"], {"result": None, "sent": 0})

    def test_the_button_and_the_form_drive_the_same_flow(self):
        flow = self.out["flow"]
        self.assertTrue(flow["submitPrevented"])  # the form never submits itself
        self.assertEqual(flow["submit"]["sent"], [{"job": "fix-a"}])
        self.assertIn("A stop was already requested for fix-a.", flow["submit"]["view"]["text"])

    def test_the_active_view_offers_stop_only_for_a_running_job_that_has_no_stop_yet(self):
        active = self.out["activeWith"]
        self.assertEqual(active["calls"], [["org/name", "r1", "fix-a"]])
        self.assertIn("Stop requested by alice at 2026-11-09 10:00Z", active["text"])
        self.assertIn("Stopped by <script>alert(1)</script>", active["text"])  # text, shown as text
        self.assertEqual(self.out["activeWithout"]["calls"], 0)  # a caller without the control gets none

    def test_the_job_view_shows_the_control_for_a_running_job_and_the_record_for_a_stopped_one(self):
        self.assertEqual(self.out["jobRunning"]["calls"], [["org/name", "r1", "fix-a"]])
        self.assertEqual(self.out["jobRunning"]["stopRow"], "Stop")
        stopped = self.out["jobStopped"]
        self.assertEqual(stopped["calls"], 0)
        self.assertIn("Stopped by alice", stopped["text"])
        self.assertIn("Reason: <script>alert(1)</script>", stopped["text"])
        for tag in ("script", "img", "iframe", "style"):
            self.assertNotIn(tag, stopped["tags"])
        self.assertEqual(self.out["jobLedger"], {"calls": 0, "stopRow": 0})


if __name__ == "__main__":
    unittest.main()
