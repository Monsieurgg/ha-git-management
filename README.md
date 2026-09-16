# Homelab Git Management

🇬🇧 [English version](README.en.md)

Synchronisation contrôlée, à sens unique (pour l'instant), entre un dépôt
GitHub et votre configuration Home Assistant : clone → comparaison →
revue → confirmation → déploiement → vérification → rollback automatique
en cas d'échec.

[![Buy me a beer](https://img.shields.io/badge/Buy%20me%20a%20beer-%F0%9F%8D%BA-orange?style=for-the-badge)](https://www.buymeacoffee.com/Monsieurgg)

## Pourquoi cet add-on ?

Les outils d'IA générative de code (assistants capables d'écrire du YAML,
des dashboards Lovelace, des automatisations…) peuvent aujourd'hui générer
ou modifier votre configuration Home Assistant directement dans un dépôt
GitHub. C'est puissant, mais ça pose une vraie question : comment faire
passer ce contenu généré vers votre installation Home Assistant *en
production*, sans jamais risquer de casser votre configuration en direct,
et sans que ce soit pénible à tester ?

C'est exactement le rôle de cet add-on : un pont sûr et explicite entre
Git et Home Assistant. Vous (ou votre IA) écrivez et committez dans le
dépôt ; cet add-on compare chaque fichier mappé avec votre configuration
réelle, vous montre précisément ce qui diffère, et ne déploie que ce que
vous confirmez explicitement — avec sauvegarde automatique et retour en
arrière si quelque chose ne va pas. Résultat : un aller-retour rapide et
sans risque entre génération de contenu et test réel, aussi souvent que
nécessaire.

## Ceci est une Application Home Assistant (Add-on) — pas une intégration HACS

Cette distinction compte, car elle change la façon d'installer ce projet :

- **HACS** distribue des `custom_components` — des intégrations Python qui
  s'exécutent *à l'intérieur* du processus Home Assistant Core et ajoutent
  des entités/appareils.
- **Ce projet est une Application/Add-on** : un conteneur Docker séparé,
  géré par le Supervisor, avec sa propre interface web accessible via
  l'Ingress de Home Assistant. Il ne s'exécute jamais à l'intérieur de
  Home Assistant Core et n'a ni dossier `custom_components`, ni
  `manifest.json`, ni config flow — rien de tout cela ne s'applique ici.

Cela signifie aussi que vous ne trouverez **pas** cet add-on en cherchant
dans HACS, et qu'il ne s'installe pas par ce biais. Voir
[Installation](#installation) ci-dessous pour la vraie procédure.

## Ce que ça fait

1. Génère sa propre paire de clés SSH au premier démarrage et affiche la
   moitié publique dans son interface web, prête à coller sur GitHub comme
   Deploy Key **en lecture seule**.
2. Clone votre dépôt (lecture seule) dans un volume persistant.
3. Compare chaque fichier/dossier que vous mappez entre le clone Git et
   votre configuration Home Assistant réelle (octet par octet, avec une
   classification "équivalent" tolérante pour les différences de fin de
   ligne/BOM uniquement).
4. Affiche le résultat et, uniquement pour les mappings configurés en
   `direction: git_to_ha` où une vraie différence existe, propose un
   bouton **Déployer**.
5. Au clic, après confirmation dans le navigateur, sauvegarde le fichier
   actuel, écrit le nouveau de façon atomique, vérifie le résultat octet
   par octet, et restaure automatiquement la sauvegarde si cette
   vérification échoue.

Rien n'est comparé ni déployable tant que vous ne l'avez pas configuré
explicitement — par défaut, l'add-on ne gère rien du tout.

## Installation

Ajout en un clic (nécessite que [My Home Assistant](https://my.home-assistant.io/)
soit lié à votre instance) :

[![Ajouter ce dépôt à votre Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FMonsieurgg%2Fha-git-management)

Ou manuellement :

1. Dans Home Assistant, allez dans **Paramètres → Modules complémentaires
   → Boutique des modules**.
2. Cliquez sur le menu **⋮** (en haut à droite) → **Dépôts**.
3. Ajoutez cette URL : `https://github.com/Monsieurgg/ha-git-management`
4. Trouvez **Homelab Git Management** dans la boutique et installez-le.
5. Démarrez-le une première fois. Ouvrez son **Web UI** (panneau Ingress)
   — un écran de configuration initiale s'affiche avec une clé SSH
   publique.
6. Sur GitHub, allez dans votre dépôt → **Settings → Deploy keys → Add
   deploy key**, collez la clé, et laissez "Allow write access"
   **décoché** (cet add-on n'a besoin que d'un accès en lecture).
7. Dans Home Assistant, ouvrez l'onglet **Configuration** de l'add-on et
   renseignez :
   - `github_repository` : `owner/repository`
   - `github_branch` : par exemple `main`
   - `mappings` : les fichiers/dossiers à gérer (voir ci-dessous) — ou
     utilisez le bouton **+ Ajouter un mapping** directement dans le
     tableau de bord de l'add-on, avec un navigateur de fichiers intégré.
8. Redémarrez l'add-on.

## Configurer les mappings

Chaque mapping est de la forme :

```yaml
mappings:
  - id: dashboard_main
    kind: file            # "file" (par défaut) ou "directory"
    ha_path: /config/dashboard.yaml
    git_path: home-assistant/dashboard.yaml
    direction: git_to_ha   # la seule direction implémentée à ce jour
```

- `id` : un identifiant court, lettres/chiffres/`_`/`-` uniquement, utilisé
  comme cible de déploiement.
- `ha_path` : chemin absolu sous `/config` (votre répertoire de
  configuration Home Assistant réel).
- `git_path` : chemin relatif à la racine du dépôt.
- `direction` : `git_to_ha`, `ha_to_git`, ou `bidirectional`. Seul
  `git_to_ha` est implémenté dans cette version — les deux autres sont
  acceptés par le schéma de configuration pour la compatibilité future,
  mais l'add-on refuse de démarrer avec une erreur claire si vous en
  configurez une, plutôt que de ne rien faire silencieusement.

Les fichiers `configuration.yaml`, `scripts.yaml`, `automations.yaml` et
`scenes.yaml` propres à Home Assistant sont toujours protégés contre un
déploiement `git_to_ha`, quel que soit le mapping — vous pouvez toujours
les mapper pour la *comparaison* (voir les écarts), mais jamais pour un
déploiement automatique.

## Sécurité

- GitHub n'est jamais atteint qu'en lecture seule, via une Deploy Key
  générée et stockée entièrement dans le volume de données persistant de
  l'add-on — jamais intégrée à l'image, jamais transmise ailleurs ;
- le clone Git n'est jamais mis à jour autrement qu'en fast-forward ; un
  clone modifié localement ou divergent fait refuser la suite à l'add-on ;
- chaque déploiement exige, en même temps : un mapping en
  `direction: git_to_ha`, une différence réellement détectée, et une
  confirmation explicite dans le navigateur ;
- chaque écriture est précédée d'une sauvegarde persistante, réalisée de
  façon atomique (`os.replace`), et vérifiée octet par octet ; un échec de
  vérification déclenche un rollback automatique ;
- le déploiement de dossiers entiers n'est volontairement pas pris en
  charge (comparaison uniquement) — une réduction de risque délibérée ;
- l'interface web n'affiche jamais le contenu des fichiers ni aucun
  secret, uniquement l'état de comparaison ;
- l'add-on n'a pas de port réseau propre : il n'est joignable que via
  l'Ingress de Home Assistant, qui exige une session utilisateur
  administrateur authentifiée. Voir
  [`homelab_git_management/DOCS.md`](homelab_git_management/DOCS.md) pour
  le détail complet du modèle d'exposition.

## Soutenir ce projet

Si ça vous évite de copier-coller du YAML à la main, vous pouvez m'offrir
une bière : [buymeacoffee.com/Monsieurgg](https://www.buymeacoffee.com/Monsieurgg).

## Trouver cet add-on

Il n'existe pas de découverte automatique façon HACS pour les dépôts
d'Add-on Home Assistant tiers — vous (ou quiconque utilise ce projet)
ajoutez toujours l'URL du dépôt manuellement, comme décrit ci-dessus. Voir
`DOCS.md` pour en savoir plus sur la visibilité et les endroits où ce
genre de projet peut raisonnablement être référencé.

## Documentation

Voir [`homelab_git_management/DOCS.md`](homelab_git_management/DOCS.md)
pour la documentation complète de l'add-on et
[`CHANGELOG.md`](homelab_git_management/CHANGELOG.md) pour l'historique
des versions.
