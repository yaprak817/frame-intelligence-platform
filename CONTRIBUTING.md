# Contributing to Frame Intelligence Platform

## Development Workflow

All changes must follow the repository development workflow.

1. Create or select a GitHub Issue.
2. Create a branch from `main`.
3. Implement the change.
4. Add or update tests.
5. Run local quality checks.
6. Commit using Conventional Commit style.
7. Push the branch.
8. Open a Pull Request.
9. Merge only after CI checks pass.

## Branch Naming

Use the following prefixes:

- `feat/` - New features
- `fix/` - Bug fixes
- `chore/` - Repository and infrastructure work
- `refactor/` - Internal code improvements
- `docs/` - Documentation changes
- `test/` - Test-related changes

Examples:

`feat/video-upload`

`feat/frame-quality-engine`

`fix/duplicate-detection`

`chore/project-foundation`

## Commit Messages

Use Conventional Commit style.

Examples:

`feat: add video upload endpoint`

`fix: handle invalid video metadata`

`test: add frame quality tests`

`chore: configure development environment`

## Pull Requests

Each Pull Request should:

- Reference the related Issue
- Describe the implemented change
- Include tests when applicable
- Pass all CI checks
- Avoid unrelated changes