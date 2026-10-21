from pathlib import Path

from typer.testing import CliRunner

from bounded_investigator.cli import app
from bounded_investigator.knowledge_report import _valuable_limitations
from bounded_investigator.source_snippets import (
    build_code_index, extract_evidence_excerpt, extract_evidence_excerpts, report_code_excerpts,
)


def test_snippets_are_verbatim_and_strictly_cited(tmp_path: Path):
    folder = tmp_path / 'packets'
    folder.mkdir()
    (folder / 'J00.md').write_text(
        '# Test\n## F-0123 sub_405AAA，原文件 L0001–L0005 / 共 5 行\n'
        '```text\nL0001: int sub_405AAA() {\nL0002:   do_work();\n'
        'L0003:   return 1;\nL0004: }\nL0005: // marker\n```\n',
        encoding='utf-8',
    )
    index = build_code_index([folder/'J00.md'])
    item = {'canonical_id':'G:C001','behavior':'call do_work', 'action':'do_work()',
            'sink':'done','evidence':[{'location':'F-0123 L2-L3', 'observation':'do_work'}]}
    excerpt = extract_evidence_excerpt(item,index)
    assert excerpt is not None
    assert excerpt['function_id'] == 'F-0123'
    assert excerpt['code'] == 'L0002:   do_work();\nL0003:   return 1;'
    assert excerpt['source_packets'] == ['J00.md']
    assert 'imagined line' not in excerpt['code']
    assert extract_evidence_excerpt({**item,'evidence':[{'location':'F-0123 L6-L10','observation':'missing'}]},index) is None
    assert extract_evidence_excerpt({**item,'evidence':[{'location':'F-9999 L1-L3','observation':'missing'}]},index) is None
    assert report_code_excerpts({'core_behaviors':[{'source_claim_ids':['J00:C001'],**item}]},folder)



def test_multiple_excerpts_are_verbatim_and_capped(tmp_path: Path):
    folder = tmp_path / "packets"
    folder.mkdir()
    (folder / "J00.md").write_text(
        "## F-0200 sub_main\n"
        "```c\n"
        "L0010: CreateMutexA(0, 0, \"marker\");\n"
        "L0011: GetLastError();\n"
        "L0012: if (e == 183) return 1;\n"
        "L0013: return 0;\n"
        "L0020: VirtualAllocEx(h, 0, n, 0x3000, 0x40);\n"
        "L0021: WriteProcessMemory(h, p, buf, n, 0);\n"
        "L0022: CreateRemoteThread(h, 0, 0, start, 0, 0, 0);\n"
        "L0023: return 1;\n"
        "```\n",
        encoding="utf-8",
    )
    index = build_code_index([folder / "J00.md"])
    item = {
        "canonical_id": "G:C010",
        "behavior": "mutex and remote thread",
        "action": "CreateMutexA WriteProcessMemory CreateRemoteThread",
        "sink": "new thread",
        "evidence": [
            {"location": "F-0200 L10-L13", "observation": "mutex"},
            {"location": "F-0200 L20-L23", "observation": "remote execution"},
            {"location": "F-0200 L20-L23", "observation": "duplicate same range"},
        ],
    }
    excerpts = extract_evidence_excerpts(item, index, max_excerpts=3)
    assert len(excerpts) == 2
    assert any("CreateRemoteThread" in x["code"] for x in excerpts)
    assert all(x["function_id"] == "F-0200" for x in excerpts)

    report = {
        "core_behaviors": [item],
        "suspected_behaviors": [{
            **item,
            "canonical_id": "G:C011",
            "behavior": "suspected remote execution",
        }],
    }
    selected = report_code_excerpts(report, folder, max_total=2)
    assert set(selected) == {"G:C010", "G:C011"}
    assert all(len(value) == 1 for value in selected.values())


def test_report_excerpt_budget_keeps_suspected_behavior_visible(tmp_path: Path):
    folder = tmp_path / "packets"
    folder.mkdir()
    (folder / "J00.md").write_text(
        "## F-0200 sub_main\n```c\n"
        + "".join(f"L{i:04d}: ImportantCall{i}();\n" for i in range(1, 8))
        + "```\n",
        encoding="utf-8",
    )
    index = build_code_index([folder / "J00.md"])
    def item(cid):
        return {
            "canonical_id": cid,
            "behavior": cid,
            "action": "ImportantCall",
            "sink": "result",
            "evidence": [{"location": "F-0200 L1-L7", "observation": "call"}],
        }
    report = {
        "core_behaviors": [item(f"G:C00{i}") for i in range(1, 5)],
        "suspected_behaviors": [item(f"G:C10{i}") for i in range(1, 3)],
    }
    selected = report_code_excerpts(report, folder, max_total=3)
    assert len(selected) == 3
    assert any(cid.startswith("G:C10") for cid in selected)
    assert sum(len(v) for v in selected.values()) <= 3

def test_evidence_ranges_merge_across_real_packets(tmp_path: Path):
    for packet, rng in [('J11',(1,2)), ('J12',(3,4))]:
        a,b=rng
        (tmp_path/f'{packet}.md').write_text(
            f'## F-0483 sub_409EF8，原文件 L{a:04d}–L{b:04d} / 共 4 行\n'
            '```text\n' + ''.join(f'L{i:04d}: code_line_{i}();\n' for i in range(a,b+1)) + '```\n',
            encoding='utf-8',
        )
    idx=build_code_index(list(tmp_path.glob('J*.md')))
    item={'behavior':'test','evidence':[{'location':'F-0483 L3-L4','observation':'last segment'}]}
    ex=extract_evidence_excerpt(item,idx)
    assert ex is not None and 'code_line_4' in ex['code']
    assert ex['source_packets']==['J12.md']


def test_snippet_not_generated_without_packet_files(tmp_path: Path):
    assert report_code_excerpts({'core_behaviors':[]},None)=={}
    assert report_code_excerpts({'core_behaviors':[]},tmp_path)=={}


def test_boilerplate_filter_does_not_modify_original_values():
    limitations=['不能断言每次运行都会创建全部文件','目标地址未恢复']
    assert _valuable_limitations(limitations) == ['目标地址未恢复']
    assert len(limitations)==2


def test_cli_version_exposes_loaded_module_and_schema():
    output=CliRunner().invoke(app,['version'])
    assert output.exit_code==0,output.stdout
    assert '1.7.2' in output.stdout
    assert 'schema_version: 10.0' in output.stdout
    assert 'loaded_module:' in output.stdout
