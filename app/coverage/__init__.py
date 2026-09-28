"""
Stage 10C.1 — worksheet-level policy, coverage validation, column KEEP
safety matrix, cross-type identifier detector и identifier expected-count
evidence (Step A).

Application-layer слой поверх уже существующих, не изменяемых Stage 8/9/
9C/10A/10B контрактов (FieldType/Action/FieldRule, Stage 5 identifier
validators). Column-level coverage внутри уже выбранного листа остаётся
исключительной ответственностью app.anonymizer._validate_rules (Stage 8)
и здесь не переисполняется.

Ничего не реэкспортируется — импортировать нужно из конкретных
подмодулей (app.coverage.models / app.coverage.errors /
app.coverage.worksheet_policy).
"""
