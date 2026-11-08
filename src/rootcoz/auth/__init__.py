"""Authentication strategies for rootcoz.

Each strategy is one way to turn a request into an
:class:`~rootcoz.auth.base.AuthIdentity`; :mod:`rootcoz.auth.registry` decides
which of them run and in what order. The package depends on ``storage``,
``config`` and :mod:`rootcoz` logging only — never on ``rootcoz.main``.
"""
