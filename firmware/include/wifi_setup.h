#pragma once

// Connexion WiFi d'Opal Box, avec réglage depuis le téléphone.
// Essaie le dernier réseau enregistré (sinon celui de secrets.h). Si ça échoue, la boîte crée
// son propre réseau « Opal-config » : on s'y connecte avec le téléphone, une page liste les
// réseaux captés, on choisit le sien et on tape le mot de passe ; il est retenu, la boîte redémarre.
namespace wifisetup {

void connect();  // bloque jusqu'à être connecté

}  // namespace wifisetup
