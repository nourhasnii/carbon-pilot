# Instructions Copilot — CarbonPilot

## Architecture du projet

- Le projet est une application Django organisée par domaines dans `apps/` :
  `accounts`, `enterprise`, `carbon`, `objectives`, `engagement`, `analytics` et
  `recommendations`.
- La configuration Django se trouve dans `config/`, notamment dans
  `config/settings.py`.
- Le modèle utilisateur est personnalisé : `AUTH_USER_MODEL = 'accounts.User'`.
- Le tenant d'une requête doit être résolu côté serveur avec
  `company_for_user` dans `apps/carbon/services/tenancy.py`. Cette fonction est
  la source de vérité pour retrouver l'entreprise de l'utilisateur connecté.
- Le contrôle d'accès par rôle utilise le décorateur `role_required` défini dans
  `apps/accounts/permissions.py`.

## Conventions observées dans `apps/objectives`

### Vues

- Les vues fonctionnelles Django sont protégées par `login_required`, puis par
  `role_required` avec les rôles autorisés au module.
- Le tenant est résolu avec `company_for_user(request.user)` ; les objets sont
  ensuite récupérés via des helpers de queryset scopés au tenant et au rôle.
- Les vues séparent les responsabilités : résolution du tenant, contrôle
  d'accès, préparation du contexte, validation du formulaire, sauvegarde et
  redirection.
- Les requêtes POST sont limitées avec `require_POST` ou
  `require_http_methods`, utilisent le CSRF Django et affichent un message
  utilisateur avant redirection.
- Un objet hors du périmètre visible renvoie volontairement `404` afin de ne pas
  révéler son existence à un autre tenant.

### Formulaires

- Les formulaires de saisie utilisent `forms.ModelForm` et déclarent
  explicitement les champs, widgets et attributs HTML.
- Les querysets des champs relationnels sont limités à l'entreprise fournie au
  formulaire ; les restrictions propres au rôle sont appliquées dans
  `__init__`.
- Les règles métier et les validations croisées sont regroupées dans
  `clean()` et les méthodes `clean_<field>()`.
- Les champs dérivés ou contrôlés par le serveur ne sont pas proposés dans le
  formulaire.

### Services

- Les services encapsulent les règles métier et d'accès réutilisables,
  notamment `visible_objectives`, `can_manage` et les helpers de tableau de
  bord.
- Les querysets commencent par filtrer le `company` résolu, puis appliquent les
  règles de rôle et de département.
- Les traitements lourds ou spécialisés, comme la prédiction, sont isolés dans
  un sous-module de service et documentés par des docstrings.
- Les constantes métier sont nommées et centralisées dans le service concerné.

### URLs

- Chaque module possède un `urls.py` avec `app_name` et des noms de routes
  explicites.
- Les URLs utilisent `path()` et des paramètres lisibles, puis sont référencées
  dans les vues et templates avec les namespaces (`objectives:...`).

### Templates

- Les templates du module sont dans `templates/objectives/` et étendent
  `carbon/base.html`.
- Ils utilisent les blocs de layout (`title`, `eyebrow`, `heading`,
  `topbar_actions`, `content`, `scripts`) et les composants CSS `cp-*` existants.
- Les formulaires incluent `{% csrf_token %}`, des labels associés aux champs,
  l'affichage des erreurs et des liens de navigation explicites.
- Les fragments réutilisables sont extraits dans des templates préfixés par
  `_`, comme `_status.html`.
- Les actions et les liens utilisent `{% url %}` avec le namespace de
  l'application ; les données sont affichées via le contexte Django et les
  filtres de template.

### Tests

- Les tests sont regroupés dans `apps/objectives/tests.py` avec
  `django.test.TestCase`.
- Des helpers de fixture (`_member`, `_objective`, `_activity`) construisent les
  données de test ; les rôles, départements et entreprises sont testés
  explicitement.
- Les tests couvrent les règles métier, les périodes, les statuts, les
  activités approuvées ou archivées, les permissions et l'isolation entre
  entreprises.
- Tout nouveau comportement doit être accompagné de tests ciblés, y compris
  les cas négatifs et les limites de permission.

## Règles obligatoires pour le module 3

- **Multi-tenant obligatoire :** chaque requête doit être filtrée par la
  `company` de l'utilisateur authentifié. Ne jamais accepter ni utiliser un
  `company_id` fourni par le navigateur pour déterminer le tenant.
- **Pas de SQL brut :** utiliser l'ORM Django et ses querysets ; aucun SQL
  construit ou exécuté directement.
- **Aucun secret dans le code :** les secrets et identifiants sensibles viennent
  de variables d'environnement ou du mécanisme de configuration prévu ; ne pas
  les ajouter aux sources, templates ou tests.
- **Français obligatoire :** les messages utilisateur, libellés, titres,
  boutons, erreurs et textes visibles ajoutés au module 3 doivent être en
  français.
- **Accessibilité WCAG AA :** conserver une structure sémantique, des labels
  associés, un ordre de navigation cohérent, des contrastes suffisants, des
  états de focus visibles et des alternatives textuelles pour les éléments non
  textuels.
- **Tests obligatoires :** tout code ajouté doit avoir des tests couvrant le
  comportement nominal, les permissions, l'isolation multi-tenant et les
  erreurs pertinentes.
