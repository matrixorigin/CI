package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"reopen-without-PR/module"
	"reopen-without-PR/pkg/config"
	"reopen-without-PR/pkg/github"
	ihttp "reopen-without-PR/pkg/http"
	"strings"
)

// 实现思路
// 1.  加载配置
// 2.  是否为需要处理的issue
// 3.  对关闭该issue的用户进行身份校验
// 3.1 若用户是白名单用户，则直接结束，不做处理
// 3.2 若用户是issue的owner,判断是否存在对关联pr;存在则不做处理,反之
// 3.3 若用户非issue的owner,白名单用户，则直接进行后续处理

var (
	// 设置GitHub API的基本URL和认证信息
	//Examples of parameters are as follows
	//baseURL = "https://api.github.com"
	//owner = "Rosyrain"
	//repo = "rosyrain/github_action_test"
	//issueNumber = 20
	//token = "xxx"
	//assignees = "Rosyrain"
	//labelData = `no-pr-linked-test2,test3`
	//labelsNeed = `tech-request,feature,Feature,kind/feature,attention/feature-incomplete,bug/ut,Bug fix,kind/bug,kind/subtask,kind/tech-request`

	baseURL      = config.GetBaseURL()
	issueOwner   = config.GetIssueOwner()
	repo         = config.GetRepository()
	issueNumber  = config.GetIssueNumber() // 你要检查的issue编号
	token        = config.GetGithubToken() // 从环境变量中获取GitHub访问令牌
	assignees    = config.GetAssignees()
	addLabelData = config.GetAddLabels()
	labelsNeed   = config.GetLabelsNeed()
	whitelist    = config.GetWhiteList()
	closeUser    = config.GetCloseUser()
)

// task 任务处理函数-- 添加标签-->转交sukki37-->reopen issue
func task() {
	// 如果没有关联的pull request，给issue添加标签并转交给sukki37
	// 添加标签
	labelURL := fmt.Sprintf("%s/repos/%s/issues/%d/labels", baseURL, repo, issueNumber)
	labelSlice := strings.Split(addLabelData, ",")
	jsonData, err := json.Marshal(labelSlice)
	if err != nil {
		panic("json.Marshal labelSlice failed")
	}
	labelResp, err := ihttp.Request("POST", labelURL, token, bytes.NewReader(jsonData), "application/json")
	if err != nil {
		panic(fmt.Sprintf("Error adding label,err:%v", err))
	}
	if labelResp.StatusCode != http.StatusOK {
		panic(fmt.Sprintf("labelURL http.Request failed,labelStatusCode:%d", labelResp.StatusCode))
	}
	defer labelResp.Body.Close()
	fmt.Printf("Add labels %s successfully.\n", addLabelData)

	// 转交给sukki37
	assigneeURL := fmt.Sprintf("%s/repos/%s/issues/%d/assignees", baseURL, repo, issueNumber)
	assigneeData := map[string]string{"assignees": assignees}
	jsonData, err = json.Marshal(assigneeData)
	if err != nil {
		panic("json.Marshal assigneeData failed")
	}
	assigneeResp, err := ihttp.Request("POST", assigneeURL, token, bytes.NewReader(jsonData), "application/json")
	if err != nil {
		panic(fmt.Sprintf("Error creating assignee request:%v", err))
	}
	if assigneeResp.StatusCode != 201 {
		panic(fmt.Sprintf("Failed to assign a problem to a specified person,assigneeStatusCode:%d", assigneeResp.StatusCode))
	}
	defer assigneeResp.Body.Close()
	fmt.Printf("Issue labeled and assigned to %s successfully.\n", assignees)

	//重新打开issue
	reopenURL := fmt.Sprintf("%s/repos/%s/issues/%d", baseURL, repo, issueNumber)
	reopenData := map[string]string{"state": "open"}
	jsonData, err = json.Marshal(reopenData)
	if err != nil {
		panic("json.Marshal reopenData failed")
	}
	reopenResp, err := ihttp.Request("PATCH", reopenURL, token, bytes.NewReader(jsonData), "application/json")
	if err != nil {
		panic(fmt.Sprintf("Error creating reopen request:%v", err))
	}
	if reopenResp.StatusCode != http.StatusOK {
		panic(fmt.Sprintf("Failed to reopen topic,reopenStatusCode:%d", reopenResp.StatusCode))
	}
	defer reopenResp.Body.Close()
	fmt.Printf("Issue %d reopen successfully.\n", issueNumber)
	if err := github.SetOutput("send", "yes"); err != nil {
		fmt.Println("set outputs failed")
	}
}

func main() {
	// 打印相关信息
	fmt.Printf("whitelist:%s\n", whitelist)
	fmt.Printf("issueOwner:%s\n", issueOwner)
	fmt.Printf("closeUser:%s\n", closeUser)
	fmt.Printf("baseURL:%s\n", baseURL)
	fmt.Printf("repo:%s\n", repo)
	fmt.Printf("issueNumber:%d\n", issueNumber)
	fmt.Printf("assignees:%s\n", assignees)
	fmt.Printf("addLabels:%s\n", addLabelData)
	fmt.Printf("labelsNeed:%s\n", labelsNeed)

	//1.判断是否为指定labels
	labelNeedURL := fmt.Sprintf("%s/repos/%s/issues/%d/labels", baseURL, repo, issueNumber)
	labelNeedResp, err := ihttp.Request("GET", labelNeedURL, token, nil, "")
	if err != nil {
		panic(fmt.Sprintf("labelNeedUrl http.Request failed,err:%v", err))
	}
	if labelNeedResp.StatusCode != http.StatusOK {
		panic(fmt.Sprintf("connect labelNeedUrl failed,resp.statusCode:%d", labelNeedResp.StatusCode))
	}
	defer labelNeedResp.Body.Close()

	lbody, err := io.ReadAll(labelNeedResp.Body)
	if err != nil {
		panic(fmt.Sprintf("Error reading issue labels response body:%v", err))
	}

	var labels []module.Label
	err = json.Unmarshal(lbody, &labels)
	if err != nil {
		panic(fmt.Sprintf("Error unmarshalling JSON:%v", err))
	}

	// 去除方括号和引号，然后按逗号分割为字符串切片
	labelsNeedSlice := strings.Split(labelsNeed, ",")

	// 创建一个映射来快速检查是否存在相同的标签
	needMap := make(map[string]bool)
	for _, need := range labelsNeedSlice {
		needMap[need] = true
	}

	hasSame := false
	for _, label := range labels {
		if needMap[label.Name] {
			hasSame = true
			fmt.Printf("Same label found: %s\n", label.Name)
			break
		}
	}

	if !hasSame {
		fmt.Printf("this issue don`t have special labels")
		if err := github.SetOutput("send", "no"); err != nil {
			fmt.Println("set outputs failed")
		}
		return
	}

	// 2.1判断是否为白名单人员
	whiteSlice := strings.Split(whitelist, ",")

	for _, user := range whiteSlice {
		if closeUser == user {
			fmt.Printf("close issue user %s is in whitelist\n", user)
			if err := github.SetOutput("send", "no"); err != nil {
				fmt.Println("set outputs failed")
			}
			return
		}
	}

	// 进行2.2/2.3判断  -- 已经确定非白名单用户
	// 2.3非白名单非issue的owner直接进行处理
	if closeUser != issueOwner {
		fmt.Printf("closeUser %s is not issueOwner %s,will exec task...", closeUser, issueOwner)
		task()
		return
	}

	// 2.2 issue_owner用户处理
	hasRelatedPR := false // 是否存在关联pr

	// 1.获取issue时间线判断是否存在pr
	// 构建请求URL
	issueURL := fmt.Sprintf("%s/repos/%s/issues/%d/timeline", baseURL, repo, issueNumber)

	// 创建请求头，包含认证信息
	issueResp, err := ihttp.Request("GET", issueURL, token, nil, "")
	if err != nil {
		panic(fmt.Sprintf("issueUrl http.Request failed,err:%v", err))
	}
	if issueResp.StatusCode != http.StatusOK {
		panic(fmt.Sprintf("connect issueUrl failed,resp.statusCode:%d", issueResp.StatusCode))
	}
	defer issueResp.Body.Close()
	fmt.Printf("get issue %d info successfully.\n", issueNumber)

	// 解析响应内容（这里省略了具体的解析过程，你可以根据需要自行处理）
	// 检查issue是否有关联的pull request
	body, err := io.ReadAll(issueResp.Body)
	if err != nil {
		panic(fmt.Sprintf("Error reading issue response body:%v", err))
	}
	var issueData []interface{}
	err = json.Unmarshal(body, &issueData)
	if err != nil {
		panic(fmt.Sprintf("Error unmarshalling JSON:%v", err))
	}

	for _, data := range issueData {
		dataMap := data.(map[string]interface{})
		typeEvent := dataMap["event"]
		if typeEvent == "cross-referenced" {
			sourceMap := dataMap["source"].(map[string]interface{})
			issueMap := sourceMap["issue"].(map[string]interface{})
			_, ok := issueMap["pull_request"]
			if ok {
				hasRelatedPR = true
				break
			}
		}
	}

	//3.进行加标签转交指定人员
	if !hasRelatedPR {
		fmt.Println("dont found related PR...")
		task()
		return
	} else {
		fmt.Println("Issue has related pull requests or don`t find need issue, no action token.")
		if err := github.SetOutput("send", "no"); err != nil {
			fmt.Println("set outputs failed")
		}
	}
}
