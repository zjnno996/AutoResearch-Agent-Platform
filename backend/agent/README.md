# EMNETS-RA research runtime

This package powers EMNETS-RA (emnets科研助手), with a six-phase research workflow and legacy 26-stage compatibility, experiment tools, evidence verification, and 210 pinned research and engineering skills.

The evidence-driven engine derives task-specific requirements, records inspectable research handoffs, runs independent specialist reviews, and returns to the stage responsible for an unmet requirement. Quality-first configuration is included in the repository's `examples/config_template.yaml`.

```bash
emnets-ra validate --config /path/to/config.yaml --no-check-paths
emnets-ra run --config /path/to/config.yaml --topic "your research question"
emnets-ra skills --verify
emnets-ra skills --stage 9 --topic "controlled experiment with independent seeds"
```

Scientific package dependencies and external model services must be configured for the task. Bundled skills provide procedures and supporting resources; installing them does not install every scientific package or provide external credentials.

See the [project documentation](https://github.com/zjnno996/AutoResearch-Agent-Platform) for configuration, the web interface, source attribution, and evaluation limits. Internal evidence coverage is not an external research benchmark score.
