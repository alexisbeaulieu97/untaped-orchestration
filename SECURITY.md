# Security policy

Report a vulnerability privately through GitHub's security-advisory interface
for `alexisbeaulieu97/untaped-orchestration`. Do not open a public issue with
store contents, filesystem paths, credentials, private repository names, or a
working exploit.

The supported line is the latest released version. Until 0.1.0 is published,
the project has no supported release. Local orchestration data may contain
sensitive roadmap information; public stores are decision-only by policy, but
operators remain responsible for repository visibility and committed content.

Normal CLI failures expose only owned structured diagnostics. Typed failures
retain their exact public diagnostic; unexpected exceptions become a generic ORC002
without the internal exception string. Mutation write failures may also include
a bounded failure receipt naming intended and acknowledged changed paths, so
operators should treat failure output as repository-sensitive. A traceback is
available only when an operator explicitly supplies `--debug`.
