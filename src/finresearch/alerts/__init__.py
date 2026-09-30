"""Alert rules for every asset kind (roadmap item 10): a metric registry per kind, the computations behind each
metric, and the engine that fires an alert once when a rule's condition becomes true (monitor/notify.py delivers it).

IPO *gating* rules (suggest/profile.py `Rule`, which force SKIP or add a warning to a suggestion) are separate and
unchanged; the IPO alert metrics here only notify.
"""
