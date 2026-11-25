// What a field can see of the forms around it: each SchemaForm provides a
// scope whose lookup(key) finds the value of the nearest field named `key`
// (its own mapping first, then the enclosing forms); lookup(key, accept)
// skips values `accept` rejects (e.g. Array.isArray when a list is wanted and
// a nearer field of the same name is something else).  Used by fields whose
// suggestions depend on values elsewhere (EntityValueInput, MapEditor).
export const FORM_SCOPE = Symbol('cfg-form-scope')
