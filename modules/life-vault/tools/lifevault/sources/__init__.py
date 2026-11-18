"""Source ingesters and the deterministic machinery they share.

Nothing in this package may call a model, open a network connection, or make a
judgement that cannot be written down as a rule. Everything here runs before a
model is involved, which is what makes it auditable.
"""
