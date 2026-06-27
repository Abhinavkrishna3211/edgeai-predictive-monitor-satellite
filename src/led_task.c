/*
 * led_task.c — Timer-driven LED state machine (single LED or RGB LED).
 *
 * Tick = 100 ms.  LCM(20, 10, 30) = 60 — tick wraps at 60.
 *
 *  LED_BOOT       always ON
 *  LED_FAULT      always ON   (solid = fault alarm — unmistakable)
 *  LED_CONNECTING ON ticks 0–9  / OFF ticks 10–19  (0.5 Hz, 20-tick period)
 *  LED_WARN       ON ticks 0–4 / OFF ticks 5–9     (1.0 Hz, 10-tick period)
 *  LED_OK         ON tick 0 only                    (blip, 30-tick = 3 s)
 *
 * RGB upgrade: define EPM_LED_RGB=1 in epm_config.h and set LED_PIN_R/G/B.
 * The same tick patterns apply; each state drives a specific colour.
 */

#include "led_task.h"
#include "epm_config.h"

#include "driver/gpio.h"
#include "esp_timer.h"
#include "esp_log.h"

static const char *TAG = "led_task";

/* ─── Single built-in LED (active-low) ───────────────────────────────────── */

#ifndef EPM_LED_RGB
#define EPM_LED_RGB 0
#endif

#if !EPM_LED_RGB

#define LED_ON()   gpio_set_level((gpio_num_t)ALERT_LED_PIN, 0)  /* active-low */
#define LED_OFF()  gpio_set_level((gpio_num_t)ALERT_LED_PIN, 1)

static void _gpio_init_single(void)
{
    gpio_config_t cfg = {
        .pin_bit_mask  = (1ULL << ALERT_LED_PIN),
        .mode          = GPIO_MODE_OUTPUT,
        .pull_up_en    = GPIO_PULLUP_DISABLE,
        .pull_down_en  = GPIO_PULLDOWN_DISABLE,
        .intr_type     = GPIO_INTR_DISABLE,
    };
    gpio_config(&cfg);
    LED_ON();
}

static void _apply_single(int on)
{
    if (on) { LED_ON(); } else { LED_OFF(); }
}

#else  /* EPM_LED_RGB — external common-cathode RGB LED ─────────────────── */

/* Colour table indexed by led_state_t (5 states) */
static const uint8_t _rgb_table[5][3] = {
    /* R,   G,   B */
    {255, 255, 255},   /* BOOT        — white  */
    {  0,   0, 255},   /* CONNECTING  — blue   */
    {  0, 200,   0},   /* OK          — green  */
    {255, 170,   0},   /* WARN        — yellow */
    {255,   0,   0},   /* FAULT       — red    */
};

static void _gpio_init_rgb(void)
{
    uint64_t mask = (1ULL << LED_PIN_R) | (1ULL << LED_PIN_G) | (1ULL << LED_PIN_B);
    gpio_config_t cfg = {
        .pin_bit_mask  = mask,
        .mode          = GPIO_MODE_OUTPUT,
        .pull_up_en    = GPIO_PULLUP_DISABLE,
        .pull_down_en  = GPIO_PULLDOWN_DISABLE,
        .intr_type     = GPIO_INTR_DISABLE,
    };
    gpio_config(&cfg);
    gpio_set_level((gpio_num_t)LED_PIN_R, 1);
    gpio_set_level((gpio_num_t)LED_PIN_G, 1);
    gpio_set_level((gpio_num_t)LED_PIN_B, 1);
}

static void _apply_rgb(led_state_t state, int on)
{
    if (on) {
        const uint8_t *c = _rgb_table[state];
        gpio_set_level((gpio_num_t)LED_PIN_R, c[0] > 0 ? 1 : 0);
        gpio_set_level((gpio_num_t)LED_PIN_G, c[1] > 0 ? 1 : 0);
        gpio_set_level((gpio_num_t)LED_PIN_B, c[2] > 0 ? 1 : 0);
    } else {
        gpio_set_level((gpio_num_t)LED_PIN_R, 0);
        gpio_set_level((gpio_num_t)LED_PIN_G, 0);
        gpio_set_level((gpio_num_t)LED_PIN_B, 0);
    }
}

#endif  /* EPM_LED_RGB */

/* ─── Shared state machine ────────────────────────────────────────────────── */

/* Atomic access — safe for cross-core write (wifi_task on CPU0) /
 * read (esp_timer dispatch on either core). */
static led_state_t s_state = LED_BOOT;

static void led_timer_cb(void *arg)
{
    static uint8_t tick = 0;
    led_state_t state = __atomic_load_n(&s_state, __ATOMIC_RELAXED);

    int on;

    switch (state) {
        case LED_BOOT:       on = 1;                 break;  /* solid ON — startup     */
        case LED_FAULT:      on = 1;                 break;  /* solid ON — fault alarm */
        case LED_CONNECTING: on = (tick % 20) < 10; break;  /* 0.5 Hz blink           */
        case LED_WARN:       on = (tick % 10) < 5;  break;  /* 1.0 Hz blink           */
        case LED_OK:         on = (tick % 30) == 0; break;  /* blip every 3 s         */
        default:             on = 0;                 break;
    }

#if EPM_LED_RGB
    _apply_rgb(state, on);
#else
    _apply_single(on);
#endif

    if (++tick >= 60) tick = 0;
}

/* ─── Public API ──────────────────────────────────────────────────────────── */

void led_set_state(led_state_t state)
{
    if ((unsigned)state > (unsigned)LED_FAULT) return;
    __atomic_store_n(&s_state, state, __ATOMIC_RELAXED);
}

/* Module-level handle — prevents a second led_task_start() from creating a
 * duplicate timer that doubles every pattern's flash rate. */
static esp_timer_handle_t s_led_timer = NULL;

void led_task_start(void)
{
    if (s_led_timer != NULL) {
        ESP_LOGW(TAG, "led_task_start() called twice — ignoring");
        return;
    }

#if EPM_LED_RGB
    _gpio_init_rgb();
    ESP_LOGI(TAG, "LED task started (RGB GPIO R=%d G=%d B=%d, 5-state)",
             LED_PIN_R, LED_PIN_G, LED_PIN_B);
#else
    _gpio_init_single();
    ESP_LOGI(TAG, "LED task started (GPIO%d active-low, 5-state)",
             ALERT_LED_PIN);
#endif

    const esp_timer_create_args_t timer_args = {
        .callback = led_timer_cb,
        .arg      = NULL,
        .name     = "led_timer",
    };
    ESP_ERROR_CHECK(esp_timer_create(&timer_args, &s_led_timer));
    ESP_ERROR_CHECK(esp_timer_start_periodic(s_led_timer, 100 * 1000));   /* 100 ms */
}
