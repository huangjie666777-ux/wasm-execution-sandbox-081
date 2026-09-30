;; Tight infinite loop: must be killed by fuel and/or the wall-clock budget.
(module
  (func (export "_start")
    (loop $forever (br $forever))))
