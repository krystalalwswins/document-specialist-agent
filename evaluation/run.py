"""Fixed offline workflow dataset; scores fixture correctness, not LLM quality."""
import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from demo.learning_demo import run_case
from documents.files import inspect_document


def evaluate():
    cases = json.loads(Path(__file__).with_name('cases.json').read_text())
    results = []
    for case in cases:
        try:
            with TemporaryDirectory() as directory:
                task, storage, _ = run_case(directory, case['values'], case['format'])
                data = next(iter(storage.objects.values()))
                parsed = inspect_document('summary.' + case['format'], data)
                passed = float(parsed['preview'][1][0]) == case['expected_total'] and task.status.value == 'SUCCESS'
                results.append({'name': case['name'], 'passed': passed, 'duration_ms': task.metrics['duration_ms']})
        except Exception as exc:
            results.append({'name': case['name'], 'passed': False, 'error': str(exc)})
    return {'mode': 'offline_fixture', 'cases': results,
            'pass_rate': sum(case['passed'] for case in results) / len(results)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', help='optional local JSON report path')
    args = parser.parse_args()
    report = evaluate()
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    raise SystemExit(0 if report['pass_rate'] == 1 else 1)


if __name__ == '__main__':
    main()
