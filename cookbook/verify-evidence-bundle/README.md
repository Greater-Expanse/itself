# Verify an evidence bundle

Use this recipe at a trust boundary: a prior CI job, agent service, sandbox, or
review system produced an Itself evidence bundle, and the current job should
check it independently before consuming its decision.

```bash
itself bundle validate path/to/bundle
```

That one command checks the complete inventory and file digests, protocol
records, reference integrity, authority constraints, deterministic state
replay, artifact bindings, and exact reasoning-receipt recomputation.

The platform examples validate the repository's closed reference bundle and
write a small verification report:

- [GitHub Actions](github-actions.yml)
- [GitLab CI](gitlab-ci.yml)

Replace `ASSURANCE_BUNDLE` with the path populated by the producing job or
artifact-download step. Keep producer and verifier in separate jobs, services,
or security boundaries when independent verification matters.

The command verifies that the supplied bundle is internally coherent and has
not changed relative to its manifest. It does not prove that the producer
observed reality honestly, that its evaluator was competent, or that the
declared evidence is sufficient for your policy.
