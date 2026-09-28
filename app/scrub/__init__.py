"""
Stage 10C.2 — Metadata & Package Scrub.

Этот пакет реализует слой, который принимает уже анонимизированный
(прошедший Stage10C.1 preparation + Stage8 anonymization) workbook и
подготавливает scrubbed-кандидат для будущей внешней передачи —
устраняя/нормализуя package- и metadata-поверхности, способные пережить
Stage8 anonymization в исходном (confidential) виде.

Текущий substage — Stage 10C.2.1 (app.scrub.preflight): дешёвый
resource + ZIP/package structural preflight, выполняемый ДО какого-либо
обращения к openpyxl. Object-model scrub, package policy/inventory,
post-save validation и public orchestration — последующие substages
(Stage10C.2.2-.5), не реализованы здесь.

Ничего не реэкспортируется — импортировать нужно из конкретных
подмодулей (app.scrub.errors / app.scrub.models / app.scrub.preflight).
"""
