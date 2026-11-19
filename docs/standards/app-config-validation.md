# App config saves

The workspace validates the effective config (including framework settings)
before saving it. Saves are partial updates. Omitted keys keep their saved
values, and a JSON null clears an override, restoring the schema default
where one exists. Required fields must be present after defaults are applied.
Send JSON values in their declared types, including booleans.

An unrelated save can retain an invalid stored value. Each violation must
match one in the previous effective config at the same field path and
schema location, with the same rule and JSON value. For rules with nested
errors, the workspace compares the individual violations. Root-level
violations still block saves. Editing an invalid value or enabling a rule
that the previous config did not trigger requires satisfying that rule. If
the stored config cannot be validated, submit a valid replacement config.

Explicit `additionalProperties` policies apply as written. An app with a
config schema and no explicit policy accepts declared properties and
`patternProperties`. Existing undeclared top-level keys can be retained
unchanged or removed. To write arbitrary keys, declare
`additionalProperties: true` or a schema for those values. Apps without an
app config schema retain open config while framework settings are validated.

A rejected save returns HTTP 422 and an error identifying the field or schema
rule. The workspace leaves the saved config and running app untouched.
