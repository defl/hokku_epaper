#pragma once

typedef enum { GPIO_PORT_A = 0, GPIO_PORT_B = 1, GPIO_PORT_C = 2, GPIO_PORT_NUM } GPIO_Port;
typedef enum {
    GPIO_PIN_0 = 0, GPIO_PIN_1, GPIO_PIN_2, GPIO_PIN_3, GPIO_PIN_4, GPIO_PIN_5,
    GPIO_PIN_6, GPIO_PIN_7, GPIO_PIN_8, GPIO_PIN_9, GPIO_PIN_10, GPIO_PIN_11,
    GPIO_PIN_12, GPIO_PIN_13, GPIO_PIN_14, GPIO_PIN_15, GPIO_PIN_16, GPIO_PIN_17,
    GPIO_PIN_18, GPIO_PIN_19, GPIO_PIN_20, GPIO_PIN_21, GPIO_PIN_22, GPIO_PIN_23,
} GPIO_Pin;
typedef enum { GPIOx_Pn_F0_INPUT = 0, GPIOx_Pn_F1_OUTPUT = 1 } GPIO_WorkMode;
typedef enum { GPIO_DRIVING_LEVEL_0 = 0, GPIO_DRIVING_LEVEL_1, GPIO_DRIVING_LEVEL_2, GPIO_DRIVING_LEVEL_3 } GPIO_DrivingLevel;
typedef enum { GPIO_PULL_NONE = 0, GPIO_PULL_UP = 1, GPIO_PULL_DOWN = 2 } GPIO_PullType;
typedef enum { GPIO_PIN_LOW = 0, GPIO_PIN_HIGH = 1 } GPIO_PinState;

typedef struct {
    GPIO_WorkMode     mode;
    GPIO_DrivingLevel driving;
    GPIO_PullType     pull;
} GPIO_InitParam;

/* Test drives pin levels here (mirrors huessen's mocks/driver/gpio.h _mock_gpio[]
 * pattern); [port][pin]. Sized generously past the real 24/22/13-pin ports. */
static int _mock_gpio[3][32];

static inline void HAL_GPIO_Init(GPIO_Port port, GPIO_Pin pin, const GPIO_InitParam *param)
{ (void)port; (void)pin; (void)param; }
static inline void HAL_GPIO_WritePin(GPIO_Port port, GPIO_Pin pin, GPIO_PinState state)
{ _mock_gpio[port][pin] = (int)state; }
static inline GPIO_PinState HAL_GPIO_ReadPin(GPIO_Port port, GPIO_Pin pin)
{ return (GPIO_PinState)_mock_gpio[port][pin]; }
