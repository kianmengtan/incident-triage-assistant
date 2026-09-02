"""The server's approval-phrase guard must match the front end's.

This is the third deliberate Python/JavaScript duplication in the repository,
alongside the capability matrix and the consumer email-domain list, and it exists
for the same reason: the browser needs the rule before it makes a request.

`routeIntent` in frontend/lib/triage.mjs routes anything that reads like an
approval command to `focus_approval` -- the card, never an action. `chat.py`
intercepts the same phrasing before it invokes the model. Both halves are needed:

* without the server's copy, a client that skips the browser (curl, or a future
  mobile client) sends "approve RB-001" straight to the model;
* without the browser's copy, the round trip happens before the user is told a
  human has to decide.

If the two lists drift, one surface treats a phrase as a command and the other
treats it as a question. That is invisible until someone types the phrase the
newer list knows about, so it is asserted here rather than left to review.
"""
import pathlib
import re

import chat

ROOT = pathlib.Path(__file__).resolve().parents[1]
LIB = (ROOT / "frontend" / "lib" / "triage.mjs").read_text()


def _alternation(intent_source, index):
    """The words in the index-th `/\\b(a|b|c)\\b/` literal of routeIntent."""
    literals = re.findall(r"/\\b\((.*?)\)\\b/", intent_source)
    assert len(literals) > index, (
        f"expected at least {index + 1} word-boundary alternations in routeIntent, "
        f"found {len(literals)}"
    )
    return tuple(literals[index].split("|"))


def _route_intent_source():
    start = LIB.index("function routeIntent(")
    end = LIB.index("\n}", start)
    return LIB[start:end]


def test_the_approval_phrases_are_identical():
    js = _alternation(_route_intent_source(), 0)
    assert set(js) == set(chat.APPROVAL_PHRASES), (
        "the approval guards disagree:\n"
        f"  only in JS:     {sorted(set(js) - set(chat.APPROVAL_PHRASES))}\n"
        f"  only in Python: {sorted(set(chat.APPROVAL_PHRASES) - set(js))}"
    )


def test_the_decline_phrases_are_identical():
    js = _alternation(_route_intent_source(), 1)
    assert set(js) == set(chat.DECLINE_PHRASES), (
        "the decline guards disagree:\n"
        f"  only in JS:     {sorted(set(js) - set(chat.DECLINE_PHRASES))}\n"
        f"  only in Python: {sorted(set(chat.DECLINE_PHRASES) - set(js))}"
    )


def test_both_sides_route_approval_away_from_an_action():
    """The JS must still resolve these to focus_approval rather than to a call,
    which is the property the phrase lists exist to serve."""
    source = _route_intent_source()
    approval_branch = source[source.index("Safety first") :]
    assert approval_branch.count("focus_approval") >= 2, (
        "routeIntent must resolve both the approval and the decline alternation to "
        "focus_approval"
    )


def test_the_python_guard_matches_every_phrase_it_declares():
    """A phrase list is only a guard if the compiled pattern actually catches it.
    Multi-word entries are the risk here: a \\b around 'run it' has to survive
    re.escape."""
    for phrase in chat.APPROVAL_PHRASES:
        assert chat.APPROVAL_RE.search(f"please {phrase} now"), phrase
    for phrase in chat.DECLINE_PHRASES:
        assert chat.DECLINE_RE.search(f"please {phrase} now"), phrase


def test_ordinary_incident_questions_are_not_mistaken_for_commands():
    """The guard fails closed towards the card, so a false positive costs a user
    an answer. These are the questions the console exists to answer."""
    for question in (
        "what is broken right now?",
        "why did checkout start failing?",
        "show me the runbook for ALT-001",
        "what are the remediation steps?",
        "who was paged for this incident?",
        "how long has this been open?",
        "what changed in the last deploy?",
    ):
        assert not chat.APPROVAL_RE.search(question), question
        assert not chat.DECLINE_RE.search(question), question


def test_the_guard_is_known_to_over_match_and_does_so_safely():
    """A pinned statement of a real false positive, not an aspiration.

    "why did the pipeline stop?" is a reasonable question about an incident, and
    the guard intercepts it, because `stop` is in the decline list that
    frontend/lib/triage.mjs has always used. The reply is then a non-answer about
    approval being a human decision.

    That is left as-is deliberately. The bias is fail-safe -- over-matching
    refuses to act, where under-matching would let approval phrasing reach the
    model -- and the list is shared with routeIntent, so narrowing it changes the
    prototype's behaviour and both halves of a safety guard at once. That is a
    decision worth making on its own rather than as a side effect of building
    this surface.

    This test exists so the behaviour is a recorded choice: if the lists are ever
    narrowed, it fails and points at the reasoning instead of letting someone
    assume the over-match was accidental.
    """
    assert chat.DECLINE_RE.search("why did the pipeline stop?")
    assert chat.APPROVAL_RE.search("what should I do it seems slow")

    # And the fail-safe direction: neither phrase can reach an action, because no
    # tool in the handler mutates anything regardless of how the text was routed.
    assert not any(
        word in name
        for name in chat.TOOL_SPECS
        for word in ("approve", "decline", "trigger")
    )
