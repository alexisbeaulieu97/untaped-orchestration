## 14. Privacy and agent workflow

Public stores are decision-only in v1. Unfinished tasks stay in the private
hub, and changing a populated private store's declaration to public makes
`check` fail. There is no public-task exception or silent private-to-public
relocation.

The packaged skill instructs agents to:

1. Run `brief --format json`.
2. Use returned IDs instead of scanning files.
3. Allocate one ID before init/create and reuse it through every retry.
4. Load only needed bodies with `show`.
5. Pass revisions on every guarded mutation.
6. Never use `--force-current`.
7. Never read/edit generated views.
8. Run `check` after hand edits/recovery.
9. Verify external evidence before recording it.
10. Never place tasks in a public store.
11. Stop readiness/delivery work on incomplete federation.

