"""Bill Check — does the invoice PDF match what was entered in Bill.com?

    open bills (Bill.com)  ─┐
                            ├─ compare field by field ─► exceptions queue
    attached invoice (PDF) ─┘   (vendor, invoice #, dates, total)
        │
        └─ read independently by Claude (extract.py), never shown the
           entered values, so it cannot anchor on the clerk's typing.

Nothing here writes to Bill.com. A mismatch is a queue item for a person
to fix in Bill.com; the next run re-verifies the fix.
"""
