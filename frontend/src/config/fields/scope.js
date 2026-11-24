// What a field can see of the forms around it: each SchemaForm provides a
// scope whose lookup(key) finds the value of the nearest field named `key`
// (its own mapping first, then the enclosing forms).  Used by fields whose
// suggestions depend on an entity chosen elsewhere (EntityValueInput).
export const FORM_SCOPE = Symbol('cfg-form-scope')
