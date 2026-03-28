# Conventional Commits

When generating or suggesting commit messages, follow [Conventional Commits](https://www.conventionalcommits.org/) specification.

## Format

```
<type>[optional scope]: <description>

[optional body]

[optional footer]
```

## Type

Must be one of:
- `feat`: A new feature
- `fix`: A bug fix
- `docs`: Documentation only changes
- `style`: Changes that don't affect code meaning (formatting, missing semicolons, etc.)
- `refactor`: Code change that neither fixes a bug nor adds a feature
- `perf`: Code change that improves performance
- `test`: Adding or updating tests
- `ci`: Changes to CI/CD configuration
- `chore`: Changes to build process, dependencies, or tooling

## Scope

Optional but recommended. Examples: `optimizer`, `storage`, `plugins/tibber_iobroker`, `config`

## Description

- Start with lowercase
- Use imperative mood ("add" not "added")
- Don't end with a period
- Max 50 characters
- Be specific and descriptive

## Body

- Explain *what* and *why*, not *how*
- Wrap at 72 characters
- Separate from description with blank line

## Breaking Changes

Mark with `BREAKING CHANGE:` in footer or append `!` after type/scope:

```
feat!: redesign ledger API
```

## Examples

- `feat(optimizer): add support for time-of-use tariffs`
- `fix(storage): prevent duplicate entries in ledger`
- `docs(README): update installation instructions`
- `refactor(core)!: restructure device registry`
- `test(differential): add edge case handling`
