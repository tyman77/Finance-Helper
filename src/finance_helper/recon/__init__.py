"""Cash Proof: bank-vs-ledger reconciliation and exception detection.

Phase 1 of the fraud-detection design (see the Cash Proof design doc):
prove that every bank movement ties to a recorded ledger entry and every
ledger cash entry ties to a bank movement; everything that doesn't tie
lands in a severity-ranked exceptions queue.
"""
