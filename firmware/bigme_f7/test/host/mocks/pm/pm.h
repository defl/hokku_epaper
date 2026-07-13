#pragma once

enum { PM_MODE_HIBERNATION = 3 };

static int _mock_pm_enter_mode_called;
static int _mock_pm_enter_mode_last;

static inline int pm_enter_mode(int mode) { _mock_pm_enter_mode_called++; _mock_pm_enter_mode_last = mode; return 0; }
