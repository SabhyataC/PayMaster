from backend.chat import (_sources, _ro, find_invoices, get_invoice, get_payments,
                          get_ledger, get_audit, get_exceptions, vendor_totals)

_sources.set([])
print(find_invoices(limit=3))
print(get_invoice("INV-6001"))
print(get_payments(status="success"))
print(get_ledger("INV-1001"))
print(get_audit("INV-6001"))
print(get_exceptions())
print(vendor_totals("Kestrel"))
print("sources:", sorted(set(_sources.get())))

# read-only proof: this write must FAIL
try:
    with _ro() as conn:
        conn.execute("UPDATE invoices SET status = status")
    print("BAD: write was allowed")
except Exception as e:
    print("OK, write blocked:", type(e).__name__)