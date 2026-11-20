#include "net_traffic.h"
#include "esp_netif_net_stack.h"
#include <stdatomic.h>

/* IDF 5.5 wlanif submits each STA frame with transmit_wrap and delivers it
 * through receive. Count at those boundaries without a per-frame event, queue,
 * allocation or payload copy. SoftAP uses another esp_netif instance. */
static esp_netif_t *station;
static atomic_uint tx, rx, epoch;
void pp_traffic_attach(esp_netif_t *sta) { station = sta; } /* Before Wi-Fi starts. */
void pp_traffic_new_window(void) { atomic_fetch_add(&epoch, 1); }
pp_traffic_t pp_traffic_read(void) {
    return (pp_traffic_t){atomic_load(&tx), atomic_load(&rx), atomic_load(&epoch)};
}

esp_err_t __real_esp_netif_transmit_wrap(esp_netif_t *, void *, size_t, void *);
esp_err_t __real_esp_netif_receive(esp_netif_t *, void *, size_t, void *);
esp_err_t __wrap_esp_netif_transmit_wrap(esp_netif_t *netif, void *data, size_t len, void *buf) {
    esp_err_t result = __real_esp_netif_transmit_wrap(netif, data, len, buf);
    if (netif == station && result == ESP_OK)
        atomic_fetch_add_explicit(&tx, len, memory_order_relaxed);
    return result;
}
esp_err_t __wrap_esp_netif_receive(esp_netif_t *netif, void *data, size_t len, void *buf) {
    esp_err_t result = __real_esp_netif_receive(netif, data, len, buf);
    if (netif == station && result == ESP_OK)
        atomic_fetch_add_explicit(&rx, len, memory_order_relaxed);
    return result;
}
