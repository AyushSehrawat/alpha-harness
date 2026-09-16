/**
 * The Data Explorer's dataset filter as a tree: Category → Subcategory → Dataset. Pure, so the
 * picker only renders it; the choice itself is always a list of dataset ids.
 */

/** The facet lists the tree is built from (`CatalogFacets` fits). */
export interface TreeSource {
  categories: { id: string; name: string | null }[]
  subcategories: { id: string; name: string | null }[]
  datasets: {
    id: string
    category_id: string | null
    subcategory_id: string | null
  }[]
}

export interface Branch {
  /** Unique in the tree: the category and subcategory ids. */
  key: string
  id: string
  name: string
  /** Every dataset under this node. */
  ids: string[]
}

export interface Trunk extends Branch {
  subcategories: Branch[]
  /** Datasets without a subcategory. */
  loose: string[]
}

/**
 * Built from the datasets themselves, so every dataset sits exactly once under its own
 * category. Categories, subcategories and datasets keep the facets' order: largest first.
 */
export function buildTree(source: TreeSource): Trunk[] {
  const trunks = new Map<string, Trunk>()
  const trunkOf = (id: string | null, name?: string | null) => {
    const key = id ?? ''
    let trunk = trunks.get(key)
    if (!trunk) {
      trunk = {
        key,
        id: key,
        name: name ?? id ?? 'Uncategorised',
        ids: [],
        subcategories: [],
        loose: [],
      }
      trunks.set(key, trunk)
    }
    return trunk
  }
  for (const c of source.categories) trunkOf(c.id, c.name)
  const subcategoryName = new Map(source.subcategories.map((s) => [s.id, s.name]))
  const subcategoryRank = new Map(source.subcategories.map((s, i) => [s.id, i]))
  const branches = new Map<string, Branch>()
  for (const d of source.datasets) {
    const trunk = trunkOf(d.category_id)
    trunk.ids.push(d.id)
    if (!d.subcategory_id) {
      trunk.loose.push(d.id)
      continue
    }
    const key = `${trunk.key}|${d.subcategory_id}`
    let branch = branches.get(key)
    if (!branch) {
      branch = {
        key,
        id: d.subcategory_id,
        name: subcategoryName.get(d.subcategory_id) ?? d.subcategory_id,
        ids: [],
      }
      branches.set(key, branch)
      trunk.subcategories.push(branch)
    }
    branch.ids.push(d.id)
  }
  const rank = (b: Branch) => subcategoryRank.get(b.id) ?? Number.MAX_SAFE_INTEGER
  for (const trunk of trunks.values()) trunk.subcategories.sort((a, b) => rank(a) - rank(b))
  return [...trunks.values()].filter((t) => t.ids.length > 0)
}

export interface Ticked {
  key: string
  label: string
  ids: string[]
}

/**
 * What is ticked, said briefly: whole categories, then whole subcategories, then single
 * datasets. A dataset the tree does not have still shows, so it can be seen and removed.
 */
export function summarize(
  tree: Trunk[],
  value: string[],
  nameOf: (id: string) => string,
): Ticked[] {
  const chosen = new Set(value)
  const summary: Ticked[] = []
  const covered = new Set<string>()
  const take = (item: Ticked) => {
    summary.push(item)
    for (const id of item.ids) covered.add(id)
  }
  for (const trunk of tree) {
    if (trunk.ids.every((id) => chosen.has(id))) {
      take({
        key: `c:${trunk.key}`,
        label: `All of ${trunk.name}`,
        ids: trunk.ids,
      })
      continue
    }
    for (const branch of trunk.subcategories) {
      if (branch.ids.every((id) => chosen.has(id)))
        take({
          key: `s:${branch.key}`,
          label: `${trunk.name} › ${branch.name}`,
          ids: branch.ids,
        })
    }
  }
  for (const id of chosen) {
    if (!covered.has(id)) summary.push({ key: `d:${id}`, label: nameOf(id), ids: [id] })
  }
  return summary
}
