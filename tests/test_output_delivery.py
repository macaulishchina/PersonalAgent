"""Evidence for early speech; no text heuristics, provider calls or extra agent loop."""

import json
from pathlib import Path
import subprocess
import unittest

from assistant_runtime.core.conversation import delivery


class OutputDeliveryTest(unittest.TestCase):
    def test_pi_observer_preserves_wire_content_and_explicit_phase(self):
        module = Path(__file__).resolve().parents[1] / "src/assistant_runtime/core/pi/output_delivery.mjs"
        script = r"""
import assert from 'node:assert/strict';
const {OutputPhases, registerOutputDelivery} = await import(MODULE);
const phases = new OutputPhases();
phases.observe({type:'response.output_item.added',item:{type:'reasoning',id:'reason'}});
phases.observe({type:'response.output_item.added',item:{type:'message',id:'one',phase:'commentary'}});
phases.observe({type:'response.output_item.added',item:{type:'message',id:'two',phase:'final_answer'}});
const raw={type:'text_start',contentIndex:1,partial:{content:[]}};
const saved=JSON.stringify(raw);
const first=phases.decorate(raw);
assert.equal(first.delivery.phase,'commentary');
assert.equal(JSON.stringify(raw),saved);
phases.decorate({type:'text_start',contentIndex:3});
const delta=phases.decorate({type:'text_delta',contentIndex:3,delta:'确切答案。'});
assert.deepEqual(delta.delivery,{phase:'final',evidence:'provider',block:3});
assert.equal(delta.delta,'确切答案。');
assert.equal(phases.decorate({type:'thinking_delta',contentIndex:3,delta:'private'}).delivery,undefined);
phases.observe({type:'response.output_item.done',item:{type:'message',id:'two',phase:'commentary'}});
assert.equal(phases.decorate({type:'text_end',contentIndex:3}).delivery.evidence,'conflict');
const missing=new OutputPhases();
assert.equal(missing.decorate({type:'text_start',contentIndex:0}).delivery,undefined);
const handlers={};
registerOutputDelivery({on:(kind,fn)=>{handlers[kind]=fn}});
const notices=[];const ctx={ui:{notify:(message)=>notices.push(message)}};
handlers.message_start({message:{role:'assistant'}});
handlers.message_update({assistantMessageEvent:delta},ctx);
handlers.message_update({assistantMessageEvent:delta},ctx);
assert.equal(notices.length,1);
assert.deepEqual(JSON.parse(notices[0].split(':').slice(1).join(':')),delta.delivery);
""".replace("MODULE", json.dumps(module.as_uri()))
        subprocess.run(
            ["node", "--input-type=module", "-e", script], check=True, capture_output=True, text=True
        )

    def test_interleaved_blocks_use_unicode_offsets_and_late_phase(self):
        presentation = delivery.append(None, 0, 2, 0, None)
        presentation = delivery.append(
            presentation, 2, 3, 1, {"block": 1, "phase": "final", "evidence": "provider"}
        )
        presentation = delivery.append(presentation, 5, 1, 0, None)
        presentation = delivery.classify(
            presentation, {"block": 0, "phase": "commentary", "evidence": "provider"}
        )
        self.assertEqual(
            [(r["start"], r["end"], r["phase"]) for r in presentation["text_ranges"]],
            [(0, 2, "commentary"), (2, 5, "final"), (5, 6, "commentary")],
        )
        changed = delivery.classify(presentation, {"block": 1, "phase": "unknown", "evidence": "conflict"})
        self.assertEqual(changed["text_ranges"][1]["phase"], "unknown")
        self.assertEqual(presentation["text_ranges"][1]["phase"], "final")

    def test_missing_or_malformed_metadata_never_grants_speech(self):
        for value in (
            None,
            {"phase": "final"},
            {"block": True, "phase": "final", "evidence": "provider"},
            {"block": 0, "phase": "final", "evidence": "model_text"},
        ):
            self.assertIsNone(delivery.evidence(value))
        presentation = None
        for i in range(130):
            presentation = delivery.append(presentation, i, 1, i % 2, None)
        self.assertTrue(presentation["range_limit"])
        self.assertLessEqual(len(presentation["text_ranges"]), 128)
