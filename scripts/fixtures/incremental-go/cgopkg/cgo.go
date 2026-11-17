package cgopkg

/*
#cgo CFLAGS: -I${SRCDIR}/native
#cgo LDFLAGS: -L${SRCDIR}/native -lexternal
#include "external.h"
*/
import "C"

func Value() int { return int(C.external_value()) }
