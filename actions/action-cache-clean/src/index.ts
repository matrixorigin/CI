import * as github from "@actions/github";
import * as core from "@actions/core";


const token = core.getInput("github_token",{required: true});
let maxLiveDuration = parseInt(core.getInput("max_live_duration",{required: false}), 10);
let owner = core.getInput("owner",{required: false});
let repository = core.getInput("repository",{required: false});

const octo = github.getOctokit(token);

function setDefaultValues() {
    maxLiveDuration = !isNaN(maxLiveDuration) ? maxLiveDuration * 1000 : 24* 60 * 60 * 1000;
    owner = owner != "" ? owner : github.context.repo.owner;
    repository = repository != "" ? repository : github.context.repo.repo;
}

async function run(){
    const {data: cacheList} =  await octo.rest.actions.getActionsCacheList(
        {
            owner: owner,
            repo: repository,
            per_page: 100,
        },
    );
    if (cacheList.total_count === 0) {
        core.info(`There is not action cache in repo ${owner}/${repository}, so skip it`);
    }
    const now = new Date();
    core.info(`Now is ${now.toISOString()}`);
    for (const cache of cacheList.actions_caches) {
        if (cache.key === undefined) {
            core.warning("The Key of cache is undefined, so skip it");
            continue;
        }
        core.info(`Start process cache ${cache.key} for repository ${owner}/${repository}`);

        if (cache.last_accessed_at === undefined) {
            core.info(`The last access time of cache ${cache.key} is undefined, so delete it`);
            deleteCache(cache.key);
            continue;
        }

        const lastAccessTime = Date.parse(cache.last_accessed_at);
        const result = now.getTime() - lastAccessTime > maxLiveDuration;

        core.debug(`now.getTime() - lastAccessTime = ${now.getTime() - lastAccessTime}`)
        core.debug(`maxLiveDuration = ${maxLiveDuration}`)

        core.info(`The result of 'now.getTime() - lastAccessTime > maxLiveDuration' is ${result}`);
        if (result) {
            deleteCache(cache.key);
        }
    }
    return null;
}

function deleteCache(key: string) {
    octo.rest.actions.deleteActionsCacheByKey(
        {
            owner: owner,
            repo: repository,
            key: key,
        },
    ).then(
        (data) => {
            core.info(`Delete cache in ${owner}/${repository} with key ${key} finished,
             response status: ${data.status}`);
        }
    )

}

async function main() {
    setDefaultValues()
    try {
        await run();
    }catch (err) {
        core.setFailed(err as Error);
    }
}

main();