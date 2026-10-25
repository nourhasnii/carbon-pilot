# Module 3 — Decarbonization Investment & Project Portfolio

Documentation technique et préparation de la soutenance. Tous les chiffres de ce document ont été obtenus en exécutant le code du dépôt (branche `feature/module3-investment`) sur les données DEMO, sauf mention contraire.

## 1. Objectif métier

Une entreprise dispose d'un budget limité et de plusieurs projets de décarbonation (solaire, CVC, flotte, isolation, éclairage…). Le module répond à trois questions :

1. **Suivre** les projets, leurs investissements et leur impact (CRUD complet).
2. **Comparer** les projets avec des indicateurs financiers et climatiques (coût net, coût par tCO₂e, ROI, retour simple, réduction cumulée).
3. **Décider** : quels projets financer sous un budget donné pour éviter le plus de tCO₂e (ou obtenir le meilleur ROI), et jusqu'où ce choix est robuste.

Le besoin est une décision sous contrainte de budget. C'est un problème d'optimisation (sac à dos 0/1), pas un problème d'apprentissage automatique.

## 2. Architecture

| App | Rôle |
| --- | --- |
| `apps/investments` | CRUD, règles de saisie, calculs financiers, comparaison, classement, données DEMO |
| `apps/portfolio_ai` | Optimiseur, sensibilité, prédiction, métriques, Gemini, tâche Celery |

`portfolio_ai` importe `investments` ; l'inverse est interdit. Chaque requête est filtrée par l'entreprise de l'utilisateur (`company_for_user(request.user)`), jamais par un identifiant venant du navigateur.

Fichiers principaux :

- `apps/investments/` : `models.py`, `forms.py`, `views.py`, `urls.py`, `services/finance.py`, `services/ranking.py`, `management/commands/load_demo_investments.py`, `tests/`.
- `apps/portfolio_ai/` : `models.py`, `views.py`, `tasks.py`, `services/optimizer.py`, `sensitivity.py`, `predictor.py`, `evaluation.py`, `llm.py`, `tests/`.
- `templates/investments/`, `templates/portfolio_ai/`, `templates/403.html`, `404.html`, `500.html`, `static/js/investments-form.js`, `llm-description.js`, `llm-summary.js`, `portfolio-run.js`.

## 3. Modèles de données

Chaque modèle principal porte une clé vers `enterprise.Company` (isolation entre entreprises).

| Modèle | Champs clés | Contraintes |
| --- | --- | --- |
| `DecarbonizationProject` | company, department (optionnel), name, project_type, description, status, start_date, end_date, is_demo | nom unique par entreprise ; date de fin ≥ date de début ; département de la même entreprise |
| `Investment` | project, planned_budget, actual_cost, committed_spend, currency (TND), funding_source, date | budget prévu > 0 ; montants ≥ 0 ; dépenses engagées ≤ coût réel si renseigné |
| `ImpactAssessment` | project, estimated_tco2e_per_year, estimated_kwh_saved_per_year, annual_savings, lifetime_years, assumptions, actual_tco2e_per_year | réduction > 0 ; durée de 1 à 50 ans ; hypothèses obligatoires |
| `ProjectScenario` | project, name, investment, impact, cost_multiplier, reduction_multiplier, energy_price | nom unique par projet ; multiplicateurs entre 0,1 et 3 ; investissement et impact du même projet |
| `PortfolioRun` | company, created_by, budget, objective, status, included_statuses, selected_project_ids, total_cost, total_tco2e, explanation, metrics, duration_ms, error_message | budget > 0 ; statuts PENDING / RUNNING / SUCCESS / FAILED |

Types de projet : SOLAR, HVAC, FLEET_ELECTRIFICATION, INSULATION, LIGHTING, OTHER. Statuts : PROPOSED, APPROVED, IN_PROGRESS, COMPLETED, CANCELLED. Les montants sont en `DecimalField` (jamais `float`). L'historique est conservé : un projet peut avoir plusieurs investissements, impacts et scénarios.

## 4. Formules et convention de coût

Convention : **coût net = coût réel s'il existe, sinon budget prévu** (somme sur les investissements du projet). L'impact utilisé est le plus récent (`created_at`).

- Réduction cumulée = tCO₂e par an × durée de vie.
- Coût par tCO₂e = coût net / réduction cumulée.
- Retour simple (années) = coût net / économies annuelles.
- ROI = (économies annuelles × durée − coût net) / coût net.
- Écart prévu vs réel = écart absolu |réel − estimé| et écart relatif rapporté à l'estimation.

Les divisions par zéro renvoient `None` (jamais de valeur inventée). Arrondi à 0,01 avec `ROUND_HALF_UP`.

Valeur optimisée pour l'objectif :

- `MAX_TCO2E` : valeur = réduction cumulée (tCO₂e évitées sur la durée de vie).
- `MAX_ROI` : valeur = économies annuelles × durée − coût net.

Les projets annulés, ceux dont une donnée manque et ceux de valeur ≤ 0 sont exclus.

## 5. Optimiseur de portefeuille

Problème : choisir des projets de coût c_i et de valeur v_i tels que Σ c_i ≤ budget, en maximisant Σ v_i. C'est le sac à dos 0/1.

**Solution exacte (`solve_exact`)** : programmation dynamique. Les coûts sont convertis en unités de 100 TND (arrondis à l'unité supérieure) et le budget en unités inférieures (arrondi à l'unité inférieure). La sélection ne dépasse donc jamais le budget réel, au prix d'une solution parfois un peu moins remplie. Complexité O(n × B') avec B' = budget / 100 ; l'énumération complète serait en O(2ⁿ).

**Heuristique gloutonne (`solve_greedy`)** : tri par ratio valeur / coût décroissant, puis ajout tant que le budget le permet, avec la même convention d'arrondi. Complexité O(n log n). Rapide, mais pas toujours optimale.

**Validation (`brute_force`)** : énumération de toutes les combinaisons, limitée à 15 projets. Sert à vérifier que la solution exacte est bien optimale sur de petites instances.

**Explication** (`explain`) : texte français construit à partir des coûts et valeurs réellement calculés (projet retenu, ou exclu car son coût dépasse le budget, ou car le budget restant est insuffisant). Moins de 2 projets éligibles : `InsufficientData`, aucun résultat inventé.

## 6. Analyse de sensibilité

`run_sensitivity` refait tourner l'optimiseur exact avec :

- le coût × {0,8 ; 0,9 ; 1,0 ; 1,1 ; 1,2} (5 cas) ;
- la réduction × {0,8 ; 0,9 ; 1,0 ; 1,1 ; 1,2} (5 cas) ;
- le prix de l'énergie × {0,8 ; 1,2} (2 cas, applicables seulement à `MAX_ROI`, car il agit sur les économies annuelles).

**Score de stabilité** = part des cas applicables dont la sélection est identique à celle de référence (10 cas pour `MAX_TCO2E`, 12 pour `MAX_ROI`). Le résultat liste aussi les projets qui entrent ou sortent du portefeuille.

## 7. Prédiction (optionnelle, avec garde-fou)

`predict_impact` estime l'impact réel d'un projet à partir des projets terminés. Deux modèles scikit-learn sont comparés par validation croisée (KFold, au plus 5 plis) : `LinearRegression` et `RandomForestRegressor`. Le meilleur selon la RMSE est retenu. `predictor_metrics` calcule MAE, RMSE et R² sur un jeu de test (80 % / 20 %, graine 42).

Garde-fou : il faut au moins **10 projets terminés avec valeurs réelles et `is_demo=False`**. Sinon, `InsufficientData` est levée et la page affiche « Données insuffisantes ». Aucune prédiction n'est inventée.

## 8. Intégration Gemini

- Fichier : `apps/portfolio_ai/services/llm.py`. Variables d'environnement `GOOGLE_API_KEY` et `GEMINI_MODEL` (valeur utilisée : `gemini-3.5-flash-lite`). Rien en dur dans le code ni dans les logs ; `.env.example` sans valeur.
- `generate_project_description(title)` : description de 4 lignes en français, modifiable, jamais enregistrée automatiquement.
- `summarize_portfolio(run)` : résumé exécutif construit uniquement à partir des chiffres calculés ; interdiction d'inventer un chiffre.
- Robustesse : délai de 30 s, appel `interactions.create` puis secours `generate_content`, gestion du quota (429), de la clé absente, de la réponse vide et des erreurs réseau. L'erreur technique est écrite dans les logs serveur, jamais montrée à l'utilisateur.
- **Repli** : si Gemini échoue, la page affiche l'explication de l'optimiseur et enregistre `llm_fallback_used = True`.
- Vues protégées par `@login_required` et `role_required`, POST uniquement (405 sinon), limite de 10 appels par minute et par utilisateur, JSON `{ok, text, error}`.
- Épisode utile pour le jury : `gemini-2.0-flash-lite` a été retiré (404) et un autre modèle a renvoyé un 504 ; l'application n'a pas planté et a utilisé le repli.

Temps de réponse Gemini mesuré : **à compléter** (relevé sur la page résultat, « Temps de réponse Gemini »).

## 9. Asynchronisme (Celery)

`run_portfolio_task(run_id)` calcule l'optimisation, les métriques, la sensibilité et la prédiction, puis enregistre le résultat dans `PortfolioRun`. La page résultat interroge `/portfolio-ai/runs/<id>/status/` (JSON) tant que le statut est PENDING ou RUNNING, avec un spinner `aria-live="polite"`. Toute exception passe le run en FAILED avec un message générique. `CELERY_TASK_ALWAYS_EAGER` est lue dans l'environnement : en ligne, la tâche s'exécute sans worker séparé.

Justification honnête : un sac à dos de 10 projets est quasi instantané. Celery sert à ne pas bloquer la requête HTTP quand les simulations de sensibilité (12 recalculs) ou l'entraînement du modèle deviennent plus longs.

## 10. Résultats mesurés (données DEMO)

Jeu DEMO : 12 projets fictifs (`[DEMO]`), dont 5 COMPLETED, 3 APPROVED, 2 PROPOSED, 1 IN_PROGRESS, 1 CANCELLED. Statuts inclus dans les simulations : PROPOSED, APPROVED, COMPLETED → 10 projets éligibles.

| Simulation | Projets retenus | Coût total | Budget restant | tCO₂e totales |
| --- | --- | --- | --- | --- |
| Budget 400 000 TND, `MAX_TCO2E` | 2 (Centrale solaire, Éclairage LED) | 394 500,00 | 5 500,00 | 7 245,00 |
| Budget 400 000 TND, `MAX_ROI` | 2 (mêmes projets) | 394 500,00 | 5 500,00 | 7 245,00 |
| Budget 40 000 TND, `MAX_TCO2E` | 1 (Éclairage LED) | 29 500,00 | 10 500,00 | 120,00 |

Métriques de l'optimiseur :

| Simulation | Écart glouton vs exact | États DP | Combinaisons brute force | Temps exact | Temps glouton | Validation brute force | Stabilité |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 400 000, `MAX_TCO2E` | 0 % | 25 827 | 1 024 (2¹⁰) | 5,63 ms | 0,05 ms | Oui | 0,60 (6/10) |
| 400 000, `MAX_ROI` | 0 % | 25 827 | 1 024 | 1,90 ms | 0,03 ms | Oui | 0,67 (8/12) |
| 40 000, `MAX_TCO2E` | 0 % | 267 | 1 024 | 0,03 ms | 0,02 ms | Oui | 1,00 (10/10) |

Durée totale de la tâche (optimisation + sensibilité + métriques) : 129 ms, 35 ms et 10 ms. Les temps varient d'une exécution à l'autre (le premier appel est plus lent).

Prédiction : « Données insuffisantes : il faut au moins 10 projets terminés avec valeurs réelles ». C'est le comportement attendu, car les projets DEMO sont exclus de l'entraînement (`is_demo=False`) et seuls 5 sont terminés.

**Comparaison glouton / exact sur instances aléatoires** (200 instances de 12 projets, graine 42, coûts de 20 000 à 400 000 TND, budget égal à 40 % du coût total) : la solution exacte égale la brute force dans 200 cas sur 200 ; le glouton s'écarte de l'optimum dans 91 cas sur 200, avec un écart moyen de 1,67 % et maximal de 13,70 %. Sur les données DEMO, le glouton trouve l'optimum.

**Temps de résolution selon la taille** (mesuré dans l'environnement de rédaction, valeurs indicatives, budget égal à 40 % du coût total) :

| Projets | DP exact | États | Glouton |
| --- | --- | --- | --- |
| 20 | 21,7 ms | 308 860 | 0,06 ms |
| 50 | 174,4 ms | 2 210 890 | 0,11 ms |
| 100 | 789,9 ms | 9 358 540 | 0,21 ms |

## 11. Sécurité, permissions, qualité

- **Rôles** : écriture pour `COMPANY_OWNER` et `ESG_REPORTING_MANAGER` ; lecture pour `AUDITOR` et `DEPARTMENT_MANAGER` ; `SUPPLIER` refusé ; anonyme redirigé vers la connexion.
- **Isolation** : un objet d'une autre entreprise renvoie 404 (pas 403), pour ne pas révéler son existence.
- **CSRF** : `{% csrf_token %}` sur les POST ; création, modification et suppression en POST, avec page de confirmation en GET.
- **ORM** : aucun SQL brut. Mots de passe hachés par Django.
- **Production** : `DJANGO_DEBUG`, `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS` lus dans l'environnement ; cookies de session et CSRF sécurisés quand `DEBUG=False` ; `SECURE_CONTENT_TYPE_NOSNIFF`. `.env` ignoré par Git.
- **Pages d'erreur** : `403.html`, `404.html`, `500.html` personnalisées.
- **Règle métier** : un projet `IN_PROGRESS` ne peut pas être supprimé.
- **Accessibilité** : un seul `<h1>` par page, `<main id="main">`, lien « Aller au contenu », `<label for>`, erreurs liées par `aria-describedby` et `role="alert"`, `aria-label` sur les boutons-icônes, focus visible. Score Lighthouse / axe : **à compléter** (une capture par page principale).

## 12. Tests

Commandes :

```
python manage.py check
python manage.py test apps.investments apps.portfolio_ai
coverage run --source=apps.investments,apps.portfolio_ai manage.py test apps.investments apps.portfolio_ai
coverage report
coverage html
```

Résultat mesuré : 124 tests, couverture de 95 % sur les deux apps. Les tests couvrent : validations des modèles et formulaires, finance, classement, optimiseur (égalité exact / brute force sur 200 instances aléatoires à graine fixe, budget nul, liste vide), sensibilité, prédicteur, vues (200, 302, 403, 404, POST valide et invalide, suppression en GET refusée), permissions et isolation entre entreprises, tâche Celery en mode eager, et Gemini avec `unittest.mock` (aucun appel réseau réel).

## 13. Installation et lancement

```
python -m venv venv
venv\Scripts\activate            # Windows ; sous Linux/Mac : source venv/bin/activate
pip install -r requirements.txt
copy .env.example .env           # puis renseigner GOOGLE_API_KEY et GEMINI_MODEL
python manage.py migrate
python manage.py load_demo_investments --company-id <id>
python manage.py runserver
```

Déploiement Render : `build.sh` (installation, `collectstatic`, `migrate`, `seed_superadmin`), démarrage `python -m gunicorn config.asgi:application -k uvicorn.workers.UvicornWorker`. Variables à saisir : `DJANGO_SECRET_KEY`, `DJANGO_DEBUG=False`, `DJANGO_ALLOWED_HOSTS`, `DATABASE_URL`, `GOOGLE_API_KEY`, `GEMINI_MODEL`, `CSRF_TRUSTED_ORIGINS`, `SUPER_ADMIN_EMAIL`, `SUPER_ADMIN_PASSWORD`.

## 14. Limites connues

- Arrondi des coûts à 100 TND : la solution exacte est exacte pour cette convention, pas pour les centimes.
- Le DP est pseudo-polynomial : il dépend du budget exprimé en unités de 100 TND.
- Prédiction indisponible tant qu'il y a moins de 10 projets réels terminés.
- Celery en mode eager en ligne : pas de worker séparé sur l'offre gratuite de Render, et l'instance gratuite se rendort après inactivité (premier appel plus lent).
- Gemini dépend d'un service externe : lenteur, quota ou retrait de modèle sont possibles ; le repli couvre ces cas.
- Les données DEMO sont fictives et marquées `is_demo`.

## 15. Questions probables de soutenance (25)

**CRUD et modèles**

1. *Quelles entités gère le CRUD ?* Projet, investissement, impact et scénario, tous liés à une entreprise ; chaque entité a liste, détail, création, modification et suppression.
2. *Comment valides-tu la saisie ?* Côté serveur avec `clean()` et `clean_<champ>()` dans des `ModelForm`, côté client avec les attributs HTML5 et `investments-form.js` (mêmes règles). Le serveur reste l'autorité.
3. *Pourquoi `Decimal` pour l'argent ?* `float` introduit des erreurs d'arrondi binaires ; `Decimal` donne des montants exacts.
4. *Pourquoi garder plusieurs investissements et impacts par projet ?* Pour conserver l'historique des hypothèses ; on n'écrase jamais les anciennes et on utilise l'impact le plus récent.
5. *Pourquoi interdire la suppression d'un projet en cours ?* Règle métier : on évite de perdre le suivi d'un projet engagé ; un message clair explique le refus.

**Sécurité et permissions**

6. *Comment empêches-tu de voir les données d'une autre entreprise ?* La company vient de `company_for_user(request.user)`, jamais du navigateur ; tous les querysets sont filtrés, et un test d'isolation vérifie le 404.
7. *Pourquoi 404 et non 403 ?* Pour ne pas révéler qu'un identifiant existe chez une autre entreprise.
8. *Qui peut écrire, lire, être refusé ?* Écriture : propriétaire et responsable ESG. Lecture : auditeur et responsable de département. Refus : fournisseur. Anonyme : redirigé vers la connexion.
9. *Que protège CSRF ?* Les POST contre les requêtes forgées depuis un autre site ; le jeton `{% csrf_token %}` est vérifié par le middleware.
10. *Comment gères-tu les secrets ?* Variables d'environnement (Render), `.env` ignoré par Git, `.env.example` sans valeur, clé jamais dans le code ni les logs, `DEBUG=False` en production.
11. *Pourquoi aucun SQL brut ?* L'ORM paramètre les requêtes et évite l'injection SQL.

**Optimisation et IA**

12. *Pourquoi cette IA et pas du machine learning ?* Le besoin est une décision sous contrainte de budget : l'optimisation est plus fiable et explicable qu'un modèle entraîné sur peu de données.
13. *Quel problème résout l'optimiseur ?* Le sac à dos 0/1 : maximiser les tCO₂e évités (ou le ROI) sans dépasser le budget.
14. *Quelle complexité ?* Programmation dynamique en O(n × B'), B' étant le budget en unités de 100 TND ; l'énumération complète serait en O(2ⁿ).
15. *Pourquoi arrondir les coûts à 100 TND ?* Pour que la table de programmation dynamique reste petite ; l'arrondi est prudent (coûts vers le haut, budget vers le bas), donc le budget réel n'est jamais dépassé.
16. *Comment valides-tu l'optimiseur ?* Par comparaison à l'énumération complète sur de petites instances (200 cas aléatoires, 200 égalités) et sur les données DEMO (validation « Oui »).
17. *Que vaut le glouton par rapport à l'exact ?* Il est bien plus rapide mais pas toujours optimal : sur 200 instances aléatoires, écart moyen de 1,67 % et maximal de 13,70 % ; sur les données DEMO, écart nul.
18. *À quoi sert l'analyse de sensibilité ?* À savoir si le portefeuille change quand le coût, la réduction ou le prix de l'énergie varient de ±20 % ; le score de stabilité donne la part des cas inchangés (0,60 sur la simulation à 400 000 TND).
19. *Que fais-tu sans assez de données pour prédire ?* Message « Données insuffisantes » ; on ne fabrique jamais une prédiction. Il faut au moins 10 projets terminés avec valeurs réelles.
20. *Pourquoi utiliser Celery ?* Pour ne pas bloquer la requête HTTP quand les recalculs de sensibilité ou l'entraînement deviennent longs ; le statut du run est suivi en base. Pour 10 projets, le calcul est instantané : on le dit honnêtement.

**Gemini, qualité et déploiement**

21. *Comment as-tu intégré Gemini ?* Dans une app séparée, via `llm.py`, avec la clé et le modèle en variables d'environnement, une vue POST protégée, une limite de 10 appels par minute et des tests simulés.
22. *Que se passe-t-il si Gemini est indisponible ?* L'application ne plante pas : elle affiche l'explication de l'optimiseur et enregistre `llm_fallback_used = True`. Cela s'est produit pour de vrai (modèle retiré, 504).
23. *Comment empêches-tu Gemini d'inventer des chiffres ?* Le prompt ne contient que les chiffres calculés et interdit d'en inventer ; en cas d'échec, on utilise le texte généré par l'optimiseur.
24. *Comment prouves-tu la qualité du code ?* 124 tests (unitaires, intégration, permissions, isolation) et 95 % de couverture sur les deux apps, avec rapport `coverage`.
25. *Comment prouves-tu l'accessibilité et comment déploies-tu ?* Scores Lighthouse et axe, contrastes calculés, parcours au clavier, structure sémantique ; déploiement sur Render avec `build.sh`, gunicorn et variables d'environnement.