# CarbonPilot — Module 3 : Decarbonization Investment & Project Portfolio

Ce README présente le **Module 3** de CarbonPilot (branche `feature/module3-investment`). Il aide une entreprise à suivre ses projets de décarbonation, à les comparer et à choisir le meilleur portefeuille sous contrainte de budget.

Documentation technique détaillée, métriques mesurées et questions de soutenance : [docs/module3.md](docs/module3.md).

## Ce que fait le module

| Partie | Application | Contenu |
| --- | --- | --- |
| CRUD | `apps/investments` | Projets, investissements, impacts et scénarios : création, lecture, modification, suppression. Validation serveur et client, calculs financiers (coût net, coût par tCO₂e, ROI, retour simple), comparaison et classement, données de démonstration |
| IA | `apps/portfolio_ai` | Optimiseur de portefeuille (sac à dos 0/1), analyse de sensibilité, prédiction avec garde-fou, résumé et description en français via Gemini, tâche Celery |

L'application `portfolio_ai` importe `investments`, jamais l'inverse.

## L'optimiseur en bref

- **Exact** : programmation dynamique, complexité O(n × B). Trouve toujours l'optimum.
- **Glouton** : tri par ratio valeur / coût, très rapide mais pas toujours optimal.
- **Force brute** : énumération complète (15 projets au maximum), utilisée pour valider l'exact.
- **Sensibilité** : coût, réduction et prix de l'énergie à ±20 %, avec un score de stabilité.
- **Prédiction** : seulement si au moins 10 projets terminés avec valeurs réelles, sinon « Données insuffisantes ».
- **Gemini** : rédige le texte à partir des chiffres calculés. Si l'appel échoue, l'application affiche l'explication de l'optimiseur (repli).

## Permissions et sécurité

- Écriture : propriétaire de l'entreprise et responsable ESG. Lecture : auditeur et responsable de département. Fournisseur refusé.
- Chaque requête est filtrée par l'entreprise de l'utilisateur ; l'objet d'une autre entreprise renvoie une page 404.
- Protection CSRF, ORM sans SQL brut, secrets lus dans les variables d'environnement, pages 403, 404 et 500 personnalisées.

## Installation et lancement

```
python -m venv venv
venv\Scripts\activate            # Windows ; sous Linux/Mac : source venv/bin/activate
pip install -r requirements.txt
copy .env.example .env           # puis renseigner les variables
python manage.py migrate
python manage.py createsuperuser
python manage.py load_demo_investments --company-id <id>
python manage.py runserver
```

Variables d'environnement utiles (voir `.env.example`) : `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS`, `DATABASE_URL`, `GOOGLE_API_KEY`, `GEMINI_MODEL`, `CELERY_TASK_ALWAYS_EAGER`. Ne publie jamais le fichier `.env`.

Pages principales :

- `/investments/projects/` : liste et gestion des projets
- `/investments/compare/` et `/investments/ranking/` : comparaison et classement
- `/portfolio-ai/` : lancer une simulation d'optimisation
- `/portfolio-ai/runs/` : historique des simulations

## Tests

```
python manage.py check
python manage.py test apps.investments apps.portfolio_ai
coverage run --source=apps.investments,apps.portfolio_ai manage.py test apps.investments apps.portfolio_ai
coverage report
coverage html
```

Résultat mesuré : 124 tests, environ 95 % de couverture sur les deux applications.

## Déploiement

Déploiement sur Render avec `build.sh` (installation, `collectstatic`, `migrate`) et gunicorn :

```
python -m gunicorn config.asgi:application -k uvicorn.workers.UvicornWorker
```

## Limites connues

- Les coûts sont arrondis à 100 TND pour l'optimiseur exact (convention prudente : le budget n'est jamais dépassé).
- La prédiction reste indisponible tant qu'il y a moins de 10 projets réels terminés.
- Gemini dépend d'un service externe : le repli couvre ses pannes.
- Les données de démonstration sont fictives et marquées `is_demo`.
