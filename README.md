# CarbonPilot — Module 4 : Energy Forecasting & Resource Optimization

## 1. Installation (à faire une seule fois)

Windows (PowerShell) :
    python -m venv .venv
    .venv\Scripts\Activate.ps1
Linux / macOS :
    python3 -m venv .venv
    source .venv/bin/activate

Puis :
    pip install -r requirements.txt
    python manage.py migrate
    python manage.py setup_roles
    python manage.py createsuperuser

## 2. Données DEMO (fictives, marquées is_demo)
    python manage.py load_demo_energy --password "ChoisisUnMotDePasse"
Comptes créés : demo_rse, demo_manager, demo_employee, demo_auditor

## 2 bis. Mise à jour des rôles (après chaque nouvelle version)
    python manage.py migrate
    python manage.py setup_roles

## 3. Lancer l'application
    python manage.py runserver
Ouvrir http://127.0.0.1:8000/  (connexion : demo_rse)

## 4. Tests et couverture
    python manage.py test
    coverage run --source=energy,core manage.py test
    coverage report --omit="*/migrations/*,*/tests/*,*/admin.py,*/apps.py"

## 5. Vérifications utiles
    python manage.py check
    python manage.py makemigrations --check --dry-run

## 6. Prévisions (Menu « Prévisions »)
- Nouvelle prévision : choisir un asset et un horizon (7 ou 30 jours).
- Moteur : baseline J-7, RandomForest, GradientBoosting ; métriques MAE/RMSE sur les 20 % de données les plus récentes.
- Asynchrone : par défaut les tâches Celery s'exécutent immédiatement (mode eager, sans Redis).
  Production : définir CELERY_EAGER=0, démarrer Redis, puis `celery -A config worker -l info`.


## Scénarios d'optimisation

Page « Scénarios » : on choisit un asset, une action (HVAC, éclairage…), une réduction (%) et la part de
l'asset concernée. Le calcul donne l'énergie, le coût et le CO₂e évités, avec un graphique avant/après.

- Référence : dernière prévision réussie du même horizon si elle couvre encore l'avenir, sinon moyenne par
  jour de semaine sur les 8 dernières semaines (minimum 14 jours valides, sinon calcul refusé).
- Hypothèses **par défaut, à confirmer** : prix du kWh 0,35 DT et facteur d'émission 0,5 kg CO₂e/kWh.
  Elles ne viennent pas d'une source officielle : à remplacer par votre facture STEG et une source officielle.
- Formule : économie du jour = référence × part concernée × réduction.


## Intelligence artificielle : ce que fait le module

- **Prévision à 7 ou 30 jours** : 3 modèles comparés (baseline J-7, RandomForest, GradientBoosting) sur un test
  chronologique, le meilleur est retenu (RMSE puis MAE).
- **Fourchette à 80 %** autour de chaque jour prévu, calculée à partir des écarts réels du test.
- **Graphique de validation** : prévu contre réel sur la période de test, avec la baseline.
- **Explicabilité** : importance par permutation de chaque variable (jour, saison, J-1, J-7, J-14, moyenne 7 jours).
- **Détection d'anomalies** : valeur attendue par jour de semaine (médiane 8 semaines), écart robuste (MAD, seuil 3,5)
  confirmé ou non par un Isolation Forest ; alertes avec explication, gravité et suivi (examiner, confirmer, écarter).
  Les scénarios d'optimisation sont un calcul, pas de l'IA.

## Contrôle de saisie

Règles centralisées dans `energy/validation.py`, appliquées par les modèles, les formulaires, l'import et les filtres d'URL :
caractères de contrôle et invisibles refusés, codes asset normalisés (unicité sans tenir compte de la casse), dates ni futures
ni antérieures à 2000, valeurs bornées, unité cohérente avec le type d'énergie, contenu réel des fichiers (pas seulement
l'extension), prévisions en double refusées, paramètres d'URL entiers validés, blocage du mot de passe après 5 échecs
(15 minutes). Côté navigateur, messages en français et champs signalés aux lecteurs d'écran ; le serveur revérifie tout.


## API REST et documentation Swagger

- Documentation interactive : `http://127.0.0.1:8000/api/docs/` (Swagger UI) — aussi `/api/redoc/` et `/api/schema/` (OpenAPI).
- Authentification par jeton : `POST /api/auth/token/` avec `{"username": "...", "password": "..."}` puis l'en-tête
  `Authorization: Token <jeton>` (bouton **Authorize** dans Swagger, valeur `Token <jeton>`).
- Ressources : `/api/assets/`, `/api/readings/`, `/api/forecasts/` (POST = calcul asynchrone, réponse 202),
  `/api/scenarios/`, `/api/anomalies/` (+ `POST /api/anomalies/detect/`).
- Mêmes règles que l'interface : droits par rôle (403), isolation par entreprise (404 sur les objets d'une autre société),
  mêmes contrôles de saisie (réutilisation des formulaires Django), pagination (25), limitation de débit, verrouillage
  du jeton après 5 échecs.
- Exemple : `curl -H "Authorization: Token <jeton>" http://127.0.0.1:8000/api/anomalies/?status=new`

## Configuration et production

Copier `.env.example` en `.env`. Avec `DJANGO_DEBUG=0`, l'application refuse de démarrer sans `DJANGO_SECRET_KEY`
(32 caractères minimum) ; `DJANGO_ALLOWED_HOSTS` est obligatoire. Aucun secret n'est dans le code.

## Évaluation de la détection d'anomalies

Les données réelles n'ont pas d'étiquettes ; on évalue donc sur des séries simulées où l'on injecte des anomalies connues :

    python manage.py evaluate_anomalies            # bruit 3 %
    python manage.py evaluate_anomalies --noise 0.10

Résultat (30 séries, 180 anomalies injectées) : bruit 3 % → précision 95,7 %, rappel 100 %, F1 97,8 % ;
bruit 10 % → précision 95,1 %, rappel 86,7 %, F1 90,7 %. Limite : ce sont des données simulées, plus propres
qu'une vraie courbe de consommation ; ces chiffres ne se transposent pas tels quels au réel.

## Traitement asynchrone réel (Celery + Redis)

Par défaut (développement) les prévisions s'exécutent immédiatement (`CELERY_EAGER=1`). Pour un vrai traitement différé :

    docker compose up -d                                  # Redis (une fois)
    set CELERY_EAGER=0                                    # Windows (cmd), dans CHAQUE fenêtre ci-dessous
    celery -A config worker -l info -P solo               # fenêtre 1 : le worker (« -P solo » obligatoire sous Windows)
    python manage.py runserver                            # fenêtre 2 : le site

Mesure faite : `POST /api/forecasts/` répond 202 « En attente » en 0,09 s ; le calcul (30 jours) se termine
ensuite en arrière-plan en 2,1 s. Une prévision restée « en attente » plus de 15 minutes (worker arrêté) ne
bloque plus les nouvelles demandes.
