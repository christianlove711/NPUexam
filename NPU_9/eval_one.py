"""Disposable official evaluation process; the parent owns its timeout."""
import sys
import traceback
from official import read_json, atomic_json, DATA, evaluate, settings

if __name__ == '__main__':
    request, output = sys.argv[1:]
    try:
        item = read_json(request)
        result = evaluate(read_json(DATA / (item['case'] + '.json')),
                          item['plan'], item['scene'], settings())
        atomic_json(output, {'ok': True, 'metrics': result})
    except Exception:
        atomic_json(output, {'ok': False, 'error': traceback.format_exc()[-4000:]})
