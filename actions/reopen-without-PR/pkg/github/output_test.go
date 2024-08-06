package github

import (
	"fmt"
	"testing"
)

func Test_SetOutput(t *testing.T) {
	err := SetOutput("test", "testaaa")
	if err != nil {
		fmt.Printf("err: %v\n", err)
		t.FailNow()
	}
}
