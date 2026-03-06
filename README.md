# pretix_simpleseatingplan

Simple seating plan for Pretix (2026.1.x).

Checklist déploiement production :

- Copier le dossier du plugin dans l'environnement
- pip install -e . (ou pip install .)
- Ajouter pretix_simpleseatingplan aux plugins activés de l'événement
- python -m pretix migrate
- python -m pretix collectstatic --noinput (important pour que les fichiers CSS/JS soient servis)
- Redémarrer le serveur (gunicorn/uwsgi)
