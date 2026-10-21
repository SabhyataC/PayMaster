import json
from jsonschema import Draft202012Validator

schema = json.load(open("schemas/invoice.schema.json"))
data = json.load(open("samples/ground_truth.json"))
validator = Draft202012Validator(schema)

bad = 0
for s in data["samples"]:
    e = s["expected"]
    for err in validator.iter_errors(e):
        print(f"[{s['id']}] schema: {err.message}")
        bad += 1
    if e["total"] is not None:
        calc = sum(li["quantity"] * li["unit_price"] for li in e["line_items"]) + (e["tax"] or 0)
        if abs(calc - e["total"]) > 0.01:
            print(f"[{s['id']}] sum mismatch: lines+tax={calc:.2f}, total={e['total']:.2f}")
            bad += 1
print(f"{len(data['samples'])} samples checked, {bad} problems")