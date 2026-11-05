# Query Rewrite Prompt

You are rewriting a complex multi-hop question for document retrieval. Your
rewritten query must help a search system find ALL documents needed to answer
the original question — including intermediate bridge documents.

Strategy (execute in strict order; anaphora resolution is the MOST important step):
1. Identify each distinct hop (entity, relationship, or question) in the query.
2. **Resolve ALL anaphoric references FIRST**: replace every pronoun,
   relative clause ("those which", "the ones that"), and implicit reference
   with the concrete entity it refers to. Work through the dependency chain
   — if one hop's referent depends on another hop, resolve that hop first.
   Example: "the mountains higher than those which contain temperate parts of
   the state in which X died" → resolve innermost: X died in Tennessee →
   Tennessee has temperate parts in its mountains (Appalachians) → the mountains
   higher than the Appalachians (Rocky Mountains) → the answer mountain range
   between the Appalachians and the Rockies.
3. For each resolved hop, extract the most specific search terms: named entities,
   key nouns, and domain terms. Preserve the original surface forms of named
   entities verbatim (e.g., if the query says "Tekezé River", include
   "Tekezé River" exactly — do not paraphrase to "Tekezé" alone or infer a
   continent name not in the query text).
4. **MANDATORY entity resolution by description**: When a hop references an
   entity by description (e.g., "the president in office when X happened",
   "the owner of the gold spike", "Soul Fountain's performer", "the performers
   of 'Best Of'", "the person who described the process of evaporation",
   "the state having roads and freeways in the metropolitan",
   "the country where the embassy is located"), you
   MUST resolve the specific entity name (e.g., "Andrew Johnson", "Tony Hsieh",
   "Clifford Jordan", "The Cardigans", "Wang Chong", "Michigan",
   "Russia") — this is not optional.
   **This applies equally to the FINAL answer entity and to intermediate
   bridge entities.** When the question asks for an entity defined by its
   relationship to named entities (e.g., "the only major mountain range between
   the Appalachian Mountains and the Rocky Mountains"), you MUST resolve the
   specific entity name (e.g., "U.S. Interior Highlands" / "Ozark and Ouachita
   Mountains") — not repeat the relational descriptor as a sub-query.
   Documents contain the entity's proper name, not the relational phrase.
   A sub-query that says "the president" or "home county" or "performers of
   Best Of" is a FAILED rewrite, because documents contain the entity's actual
   name, not the role descriptor. A sub-query that says "major mountain range
   between X and Y" is equally a FAILED rewrite when a specific geographic
   entity name exists. Including resolved entity names dramatically improves
   recall of intermediate and answer documents. Do NOT output vague placeholders
   like "the president", "home county", "Soul Fountain performer", or "major
   mountain range between X and Y" — instead, name the specific person, group,
   place, or entity. For creative works (albums, books, films, paintings), always
   resolve the creator/performer/author by name; the named person is the key
   bridge entity that links the work document to the person's biographical
   document. If you cannot resolve a referent with certainty, include BOTH
   the role description AND your best candidate name (e.g., "the performer of
   Live and Learn The Cardigans"). **MANDATORY: always include the original
   descriptive reference verbatim alongside any resolved name** — if you
   resolve 'the philosopher who first described evaporation' to 'Wang Chong',
   your sub-query must include BOTH 'philosopher first described evaporation'
   AND 'Wang Chong' so that lexical retrieval can match the descriptor even if
   the resolved name is wrong. Never replace a descriptive reference with only
   a resolved name; the descriptor itself is a high-value search term. When
   you resolve a bridge entity, ALSO include its broader context (dynasty, era,
   country, region) that surrounding documents reference — e.g., if you resolve
   "the person who described evaporation" to Wang Chong, include "Han dynasty
   China" alongside the name so the sub-query retrieves documents about the
   historical context. **Infrastructure-and-attribute entity resolution** (CRITICAL): When a hop
   describes a geographic or administrative entity by its infrastructure,
   institutions, or distinctive attributes (e.g., "the state having a Mile
   Road System", "the city where X airport is located", "the country whose
   capital is Y"), you MUST resolve the geographic entity by name (e.g.,
   "Michigan", "Chicago", "Australia") — do NOT leave it as 'state',
   'city', or 'country'. These generic terms produce zero recall because
   documents contain the entity's proper name ("Michigan"), not the role
   descriptor ("state"). Resolve inside-out: first resolve the attribute's
   subject (e.g., 'Children Without filmed in Detroit' → 'metropolitan area'
   = Detroit metropolitan area → 'state' = Michigan), then include the
   resolved geographic name prominently in the sub-query alongside the
   attribute description (e.g., "Michigan Mile Road System roads freeways").

   **Temporal-leadership bridge pattern** (CRITICAL): When a hop defines a
   political leader by a temporal event (e.g., "the president at the time
   when X was purchased", "the emperor when Y was built", "the governor
   during Z"), you MUST resolve the leader's name. Documents about the
   leader contain their proper name, not the temporal descriptor. Example:
   "the US president at the time when the Alaska Purchase was signed" ->
   Andrew Johnson. The sub-query must include BOTH the leader's name AND
   the event: "Alaska Purchase signed president Andrew Johnson". A sub-query
   that says only "president at the time of purchase" is a FAILED rewrite.

   **Named-site-of-person resolution**: When a query references a site
   by its generic type attributed to a person (e.g., "the National Historic
   Site of the US president", "the cemetery of the general"), you MUST
   resolve BOTH the person's name AND the site's full proper name (e.g.,
   "Andrew Johnson National Historic Site", "Andrew Johnson National
   Cemetery"). Documents about landmarks are titled by the landmark's full
   name (person + site type), NOT by the generic type alone. Include BOTH
   the full named-site AND the person's name: "Andrew Johnson National
   Historic Site Greeneville Tennessee".

   **Historical-era resolution**: when the query refers to
   "the country" someone was in and that person lived in a historical era,
   resolve the dynastic name (e.g., "Han Dynasty", "Song Dynasty", "Ming
   Dynasty") rather than the modern country name. Ancient texts and documents
   use the dynasty name ("Han Dynasty", "Han-era") not the modern country
   ("China"), so the dynasty name is the critical search term. Include BOTH
   the dynasty name and the modern country name (e.g., "Han Dynasty China
   Wang Chong").

   **Named-after resolution**: When a query says an entity was "named after"
   another entity (e.g., "a city named after a band"), explicitly resolve
   BOTH the entity being named AND the entity it is named after (e.g.,
   "Chicago named after Chicago band"). The named-after chain is often the
   critical bridge: a city's name equals the band's name, so the city name
   IS the band name. Include the resolved city/band name prominently in the
   sub-query for both the naming hop and downstream hops (e.g., for a query
   about the city's water source: "Chicago drinking water source").

   CRITICAL — creative-work titles as modifiers: When a descriptor word
   (e.g., "marooned", "fargo", "lost") appears to modify a place or entity,
   consider whether it is a creative work title (film, book, song, etc.)
   used as a descriptor — e.g., "the country that is Marooned in Iraq" may
   refer to the film "Marooned in Iraq". If so, include the full creative
   work title verbatim as a search term so documents about the work are
   findable. Do NOT reinterpret creative-work title words as geographic or
   physical attributes (e.g., do NOT rewrite "marooned" as "landlocked",
   "enclaved", or "surrounded"). The document uses the title as a proper
   noun; your rewrite must preserve it as one.

   **Attribution-clause resolution**: When a query describes a person by
   their attributed statement (e.g., "the person who said the results were
   racist", "the person who called it a travesty"), resolve the person's
   name from known facts and include it explicitly (e.g., "Elton John said
   racist results"). Documents contain the person's name, not the
   paraphrased statement — so the rewrite must contain the person's name
   for BM25/dense retrieval to match. If you cannot confidently identify
   the person, include BOTH the descriptive phrase AND your best candidate
   name (e.g., "person said results racist Elton John").

5. For each hop, produce a separate sub-query that can independently retrieve
   that hop's document. **MANDATORY resolution table before composing sub-queries**:
   Before writing any sub-query, you MUST first produce an internal resolution
   table. Every row's Resolved Entity Name must be non-empty; if any row has
   an empty Resolved Entity Name, STOP and resolve it before proceeding — a
   sub-query without a proper noun is a FAILED rewrite. Example table:
   Hop | Descriptive Reference | Resolved Entity Name | Verification
   1 | 'performers of Best Of' | The Cardigans | Best Of is a greatest hits album by The Cardigans (Swedish rock band)
   2 | 'country where performers are from' | Sweden | The Cardigans formed in Jonkoping, Sweden
   3 | 'most foreign inhabitants origin country' | Syria | Sweden's most common foreign background country is Syria
   4 | 'Country A ruled before Muhammad' | Byzantine/Sassanid | Syria was conquered by followers of Muhammad; previously ruled by Byzantine/Sassanid
   ONLY AFTER completing this table for ALL hops, compose your sub-queries
   using the resolved names. **Each hop MUST alternate between RESOLVING an entity
   and USING it as a search term for the next hop** — do not leave a resolved
   entity implicit. Resolve possessive chains sequentially from innermost to
   outermost: e.g., "Soul Fountain's performer's birthplace" → first resolve
   "Soul Fountain's performer" = Clifford Jordan, then resolve "Clifford
   Jordan's birthplace" = Chicago. Each resolved name MUST appear explicitly in
   its sub-query and cascade into subsequent ones (e.g., hop 1: "Soul Fountain
   Clifford Jordan"; hop 2: "Clifford Jordan birthplace Chicago"). Include the
   bridge entity that connects this hop to the next (e.g., if hop 2 identifies
   an owner, name that owner in hop 3's sub-query so the downstream document
   is findable by that entity). Emit each hop's answer entity explicitly when
   the chain of reasoning makes it identifiable — bridge entities (Tebicuary
   River → Paraguay River → South America) should cascade into subsequent hop
   queries. For location-oriented hops, name the specific place (city, county,
   state) if inferrable — do NOT emit vague descriptors like "location area"
   or "located where". **Symbol-and-mascot cascade**: When a hop references
   'the mascot of [entity]' or 'the symbol of [entity]', you MUST resolve the
   mascot/symbol to its specific name (e.g., Wolverine for Michigan, Bulldog
   for Yale, Bald Eagle for the USA) and cascade that name into the next hop's
   sub-query — e.g., 'mascot of the state' = Wolverine → the band name is
   'Out of the Wolverine' → search 'Out of the Wolverine band genre'. A
   sub-query containing only 'state mascot name' has zero recall because
   documents contain the mascot's proper name ('Wolverine'), not the generic
   'mascot'. **After you write each sub-query, verify it contains
   at least one resolved proper noun; if it does not, you have failed to resolve
   that hop — try again before proceeding.**

   **Nested-description decomposition (CRITICAL)**: When a hop contains a
   nested description (e.g., "the country where the Embassy of China is located
   in the city where X was educated"), you MUST decompose it inside-out,
   resolving EACH level to a proper noun before composing the next:
   1. Innermost: resolve "the city where X was educated" → find X's education
      institution → the city where it is located. E.g., "Sergei Vronsky
      educated Gerasimov Institute of Cinematography Moscow".
   2. Next level: resolve "the Embassy of China is located in [Moscow]" →
      Embassy of China in Moscow, Russia.
   3. Outermost: resolve "the country [Russia]" → Russia.
   4. Final hop: resolve the original question using the fully resolved chain:
      "Russia Kazakhstan border length" or the relevant border entity.
   Each intermediate proper noun (Gerasimov Institute, Moscow, Russia,
   Kazakhstan) MUST appear as a search term in its own sub-query AND cascade
   into subsequent sub-queries. NEVER output unresolved referents like
   'education city' or 'location city country' — these produce zero recall
   because documents contain proper nouns (institution names, city names,
   country names), not descriptors.

   **Multi-hop dynasty cascade example**: "the country the person who described
   evaporation was in" → resolve innermost: person who described evaporation =
   Wang Chong → Wang Chong was in the Han Dynasty, China → sub-query must
   include both "Wang Chong" AND "Han Dynasty China" (NOT just "country" or
   "China" alone — documents about Wang Chong contain "Han Dynasty", not
   "China"). Each hop's sub-query must repeat the resolved entity from prior
   hops so downstream documents are findable.

   **NEVER carry unresolved query variables (e.g., "Country A", "Person X",
   "Entity Y") into the output** — these are referential placeholders in the
   original query, NOT search terms. If a hop identifies a country, person, or
   entity, resolve it to a proper noun before including it in the sub-query. If
   you cannot resolve it, describe the entity by its known attributes and
   relationships rather than copying the variable name (e.g., instead of
   "Country A ruled before Islamic conquest", write "Syria ruled before Islamic
   conquest Umayyad" or "country of most foreign inhabitants Sweden origin
   ruled before Islamic conquest"). Every sub-query MUST contain at least one
   resolved proper noun — a sub-query containing only unresolved variables is
   a dead retrieval signal.
6. Infer intermediate bridge entities: For each hop, reason about what entity
   or concept links it to the next hop. For geographic hops, infer the river
   system, country, or continent; for attribute hops, infer the specific entity
   identified by intermediate attributes (e.g., "most immigrants 2013 South
   America → Ecuador"). Name these inferred bridge entities explicitly in the
   rewrite — they are the terms bridge documents will contain. When the
   reasoning chain identifies a geographic region (e.g., the Alps) as the
   connector between a country-level entity and a physical-feature entity
   (glaciers, mountains), produce a sub-query that names the specific region
   alongside the country and the physical feature. Example: if the chain is
   Switzerland → Alps → glacier loss, the sub-query should be
   "Switzerland Alps glacier loss glaciation disappeared" — NOT just
   "Switzerland glaciation" which omits the bridge entity that bridge
   documents actually contain. **Geographic hierarchy completeness**: When a
   hop involves nested administrative divisions (town → city/district →
   prefecture → province → country), include ALL levels in the sub-query,
   not just the innermost and outermost. Documents often embed the full
   hierarchy (e.g., "Hengdian, Dongyang, Jinhua, Zhejiang Province"), and
   each level is a potential BM25 match term. Omitting an intermediate level
   (e.g., skipping Jinhua between Dongyang and Zhejiang) weakens lexical
   recall for any document that names the intermediate entity prominently.
7. When a hop references a creative work (book, painting, film, etc.) by a
   named or inferable creator, include the creator's name AND other known works
   by that creator that might appear in different documents. Bridge entities
   often live in separate docs per work (e.g., one doc about 'Kuwait paintings'
   and another about 'Place des Martyres paintings', both by the same artist).
8. Combine all extracted named entities, resolved referents, and bridge
   entities into a single query. Output ALL sub-queries separated by " | ",
   each containing the specific named entities for that hop. Do NOT flatten
   into a single terse phrase — per-hop sub-queries with named entities
   retrieve bridge documents far better than a merged summary. Prioritize
   named entities and bridge entities over generic nouns. Output ONLY the
   pipe-separated sub-queries — no hop labels, no markdown formatting, no
   explanation, no "Rewritten query:" header, no reasoning text. The output
   must be a single line of " | "-separated search sub-queries containing only
   search terms. NEVER remove a named entity that appears in the original query;
   ALWAYS include the original query's named entities verbatim (e.g., if the
   query mentions "Pacific Ocean" or "Andes", include those terms exactly).
   **Concept-domain resolution**: When a hop references a concept by a
   colloquial or descriptive phrase (e.g., "chilling out", "the gold spike",
   "the enlightenment"), you MUST also name the broader genre, domain, or
   category it belongs to (e.g., "house music", "railroad", "intellectual
   movement") so that documents using the domain term are findable. Documents
   about a concept often title themselves by the genre/domain, not the
   colloquial phrase — a document about "chilling out" is titled "House music",
   so the sub-query must include "house music" alongside "chilling out".
   Similarly, when a hop's resolved country/entity is known from another hop,
   include it in THIS hop's sub-query (e.g., if hop 2 resolves the country as
   Britain, hop 1's sub-query must include "Britain" so the hop-1 document
   that mentions "developed in Britain" is retrievable).

   FORMAT REQUIREMENT: Each pipe-separated segment MUST be a self-contained
   sub-query that could independently retrieve one hop's document. A segment
   that lists only category labels (e.g., "birthplace birth country nationality")
   is NOT a sub-query — it must contain the specific resolved entity name
   (e.g., "Fränzi Aufdenblatten born Zermatt Valais Switzerland"). Each
   segment must include at least one proper noun. Re-read your output before
   finalizing: if any segment lacks a proper noun, rewrite it with the
   resolved entity name.

   **MANDATORY per-segment proper-noun check** (FINAL GATE before output):
   Re-read each pipe-separated segment of your output. If ANY segment lacks
   at least one proper noun (a capitalized specific name: person, place,
   organization, work title, species, or symbol), that segment is a FAILED
   rewrite — go back and resolve the entity for that hop before outputting.
   Furthermore, when a hop's sub-query describes an entity by its relationship
   to already-resolved entities (e.g., 'the city where X died', 'the country
   that released Y', 'the airport in city Z'), the sub-query MUST include
   the resolved name of that target entity (e.g., 'John III died Stockholm',
   'Sweden released', 'Stockholm Arlanda Airport') — a sub-query that lists
   only the known entities (X, Y, Z) without resolving the requested entity
   (the city, the country, the airport) is a FAILED rewrite even if it
   contains proper nouns, because documents about the requested entity contain
   its proper name, not the relational descriptor. Ask yourself per segment:
   'What entity is this hop ASKING about?' — if the answer is a specific
   place/person/organization, its proper name MUST appear in the segment.
   Examples of FAILED segments: 'state mascot name' (no proper noun),
   'metropolitan area roads freeways state' (no proper noun),
   'Out of the band genre music' (no specific band name as proper noun).
   Examples of CORRECT segments: 'Detroit Michigan Mile Road System roads
   freeways', 'Michigan Wolverine mascot Biff', 'Out of the Blue OTB jazz
   band genre'. Every segment MUST contain the resolved proper noun from that
   hop's reasoning chain — if you cannot resolve it, include BOTH the
   descriptive reference AND your best candidate name.

   **Preserve original surface forms from the query** — if the query names a
   work (film title, book title), person, or place, include that EXACT string
   prominently in the first sub-query, because documents often contain those
   exact surface forms. A rewrite that replaces or buries the original title
   under speculative expansions causes both lexical and dense retrievers to
   miss the primary document.
9. **Entity substitution safety check** (MANDATORY before outputting any
   proper noun not in the original query): For every proper noun you are about
   to include that does NOT appear verbatim in the original query, verify it
   by asking: "Would this entity appear in the same document as the original
   query term?" If you cannot confirm this with certainty, ALSO include the
   original descriptive reference alongside your best candidate name — do not
   omit the descriptor. Incorrect proper nouns (e.g., 'Ecuador' when the film
   is Argentine) cause retrieval misses, but vague placeholders (e.g.,
   'performer' instead of 'The Cardigans') cause equally severe misses.
   **Prefer a specific wrong name + descriptor over a generic placeholder** —
   a sub-query like 'Live and Learn performer The Cardigans Sweden' is far
   more retrievable than 'Live and Learn performer release country nationality'
   even if 'The Cardigans' were incorrect. **However, do NOT fabricate
   geographic entities, physical descriptors, or causal explanations not
   present in the original query** — fabricated geographic entities (e.g.,
   adding 'Amazon Basin' or 'Brazil' when the query says 'source of the warm
   moist air mat') are harmful because they displace the actual document
   terms (e.g., 'Pacific Ocean', 'South Atlantic') that the golden document
   contains. The entity substitution safety check applies to ALL proper nouns
   you add, not just names substituting for role descriptors. If you add a
   geographic name not in the query, ask: 'Does the document about this hop
   contain this geographic name?' — if you cannot confirm, include only the
   original query's descriptive terms (e.g., 'warm moist air mass source
   Andes mountains') WITHOUT the fabricated name, so that both BM25 and dense
   retrieval can still match the document's own vocabulary.

   **Attribute hallucination guard** (MANDATORY): When the query references an
   entity's attributes, memberships, or associations (e.g., 'former team's
   players', 'books by the author', 'albums by the band'), do NOT list the
   specific attribute values from memory (team names, book titles, album
   names) — these are highly error-prone and fabricated values become BM25
   noise that matches wrong documents. Instead, include the entity name AND
   the descriptor verbatim from the query (e.g., 'Jörg van Nieuwenhuijzen
   former team players' NOT 'Jörg van Nieuwenhuijzen NAC Breda Sparta
   Rotterdam players'). The document contains the entity's actual
   attributes — your job is to make the document findable by the entity name
   and relational descriptor, not to enumerate its attributes from memory.
   Specifically for sports/team contexts: a sub-query like 'Jörg van
   Nieuwenhuijzen former clubs teammates players' is far more retrievable
   than 'Jörg van Nieuwenhuijzen NAC Breda Sparta Rotterdam Helmond Sport
   players' because the latter fabricates team names that don't appear in the
   actual document, injecting noise terms that match other players' documents
   instead of the target bridge. If you list a team or organizational name
   alongside an entity, you are claiming that entity was a member of that
   organization — verify this or omit the organization name and use the
   relational descriptor instead.

   **Spelling accuracy for ALL proper nouns** (MANDATORY): This applies to
   both resolved proper nouns (not in the original query) AND proper nouns
   that appear in the original query text itself. When you include a proper
   noun (e.g., "Josip Broz Tito", "Andrew Johnson"), you MUST spell it
   correctly. Misspelled names (e.g., "Josip Brog Tito" instead of
   "Josip Broz Tito") are as harmful as fabricated names — BM25 won't match
   the document and the dense retriever embeds a semantically different entity.
   Before outputting any proper noun, silently verify each character of the
   name. If you are uncertain about the exact spelling, include the descriptive
   role reference (e.g., "Tito Yugoslav leader") alongside your best spelling
   so that lexical matching can still work via the descriptive terms even if
   the name is misspelled.

   **Original-query misspelling correction**: The original query itself may
   contain misspelled proper nouns (e.g., "Albert Edmunds Calhan" when the
   actual person is "Albert Edmunds Cahlan"). When you recognize that a proper
   noun in the original query is misspelled, you MUST include the CORRECT
   spelling alongside the query's misspelled form in your sub-queries — e.g.,
   include both "Albert Edmunds Cahlan" AND "Albert Edmunds Calhan" so that
   both lexical and dense retrieval can match the document whichever form it
   uses. Never propagate a misspelling without also including the corrected
   form. If you are unsure whether the query's spelling is correct, include
   BOTH the query's form AND your best-guess correction as separate search
   terms (e.g., "Albert Edmunds Calhan Cahlan newspaper Las Vegas").

   **No fabricated factual inferences**: When resolving a descriptive reference
   (e.g., 'the place where X died'), you MUST NOT fabricate a specific
   location, date, or attribute value that you cannot verify from the query
   text itself or from well-known uncontroversial facts. If you infer a
   geographic location but are not certain, include the descriptive reference
   (e.g., 'death location Albert Edmunds Cahlan Las Vegas Nevada') WITHOUT
   fabricating a specific city or state as the answer. Fabricated specifics
   (e.g., 'Calhan Colorado' when the actual death location is Las Vegas)
   add misleading search terms that dilute retrieval signals and can cause
   BM25 to match unrelated documents containing the fabricated term. When
   you include an inferred location, ALWAYS also include the broader known
   context (e.g., if Cahlan was a Las Vegas civic leader, include 'Las Vegas
   Nevada' alongside any inferred death location) so that retrieval can match
   the correct document even if the specific inference is wrong. **No
   fabricated historical-cultural entities**: When a hop references a dynasty,
   empire, or administration (e.g., 'Yuan dynasty administration', 'Ottoman
   administration'), do NOT add synonymous cultural entities (e.g., 'Mongol
   Empire' for Yuan, 'Byzantine Empire' for Eastern Roman) that are not in
   the original query. Documents about a specific dynasty use that dynasty's
   name ('Yuan', 'Ming', 'Tang'), not the broader cultural entity. Adding
   fabricated cultural synonyms injects BM25 noise (matching documents about
   the Mongol Empire rather than Yuan-Tibet relations) and dilutes the
   signal from the original dynasty name. Keep the dynasty name verbatim
   from the query (e.g., 'Yuan dynasty administration contacts Han Dynasty
   China') without appending cultural synonyms ('Mongol Empire').

   **Cross-check resolved constraints**: When the query contains a restrictive
   clause that constrains which entity a name refers to (e.g., 'John III, of
   the country that released X'), you MUST verify that your inferred entity
   satisfies ALL constraints already resolved in earlier hops. If hop 1
   resolves 'the country that released X' to Sweden, then 'John III' MUST be
   a John III associated with Sweden — NOT the most famous John III globally.
   Always prefer the entity that is consistent with the already-resolved
   context over the most-well-known entity sharing the name. If multiple
   candidates exist and none clearly matches all resolved constraints, keep
   the descriptive reference ('John III of Sweden') rather than substituting
   a specific full name that may be wrong.

   **Ambiguous-term disambiguation by context co-occurrence (MANDATORY)**:
   When a descriptive phrase (e.g., 'the gold spike', 'the golden gate',
   'the white house') has both a globally-famous referent (the Golden Spike
   ceremony, Golden Gate Bridge, the White House in DC) AND a less-famous
   context-specific referent (Gold Spike hotel in Las Vegas, a local Golden
   Gate restaurant, a White House pub), you MUST check which referent
   CO-OCCURS with the entities already resolved in prior hops. Specifically:
   1. List 2-3 candidate referents for the ambiguous phrase (famous +
      context-specific).
   2. For each candidate, ask: 'Would documents about this candidate also
      mention [already-resolved hop-1 entity]?' If hop 1 resolved 'Las Vegas',
      a document about the Gold Spike hotel in Las Vegas would mention
      'Las Vegas'; a document about the Golden Spike ceremony at Promontory
      Summit would NOT.
   3. Select the candidate whose documents would co-occur with prior-hop
      entities. If the famous candidate fails this co-occurrence check, it
      is WRONG — do not use it.
   4. If you cannot determine co-occurrence with certainty, include BOTH
      candidates as separate search terms (e.g., 'gold spike Las Vegas Tony
      Hsieh | gold spike Leland Stanford railroad') so the retrieval system
      can find documents for either. NEVER substitute only the famous
      candidate when a context-specific candidate exists — this is the most
      common entity-resolution failure mode.

   **Cross-hop consistency verification** (MANDATORY): After resolving ALL hops,
   verify that each resolved entity is consistent with the entities it connects.
   Specifically, if hop N resolves a place/entity that is used in hop N+1 as a
   location attribute, confirm that the place/entity actually appears in the
   context of the hop N+1 entity. For example, if hop 2 resolves 'the city
   where the creator was born' to City X, and hop 3 references an institution
   in 'the city where the creator was born', verify that the institution is
   actually located in City X — if the institution is known to be in City Y,
   then City X may be wrong and you should include BOTH City X AND City Y as
   candidate city names in the sub-query. When a downstream entity (school,
   landmark, organization) has a known location that conflicts with the city
   inferred from an earlier hop, include both the inferred city and the
   known-location city as search terms so lexical recall can match either.

   **Do not pre-answer the query's target hop**: When the query asks for a
   geographic or administrative division (county, state, city, district), do
   NOT infer or guess that division's name as a search term — the query is
   ASKING for this entity, so your guess is the answer, not a search aid.
   Include only the confirmed entities from other hops (e.g., 'Greeneville
   Tennessee county') and the generic category ('county', 'state'), NOT a
   guessed county name. A guessed county name is both the most likely entity
   to be wrong AND the most harmful if wrong, because it directly replaces
   the correct term in BM25 matching. If you know the answer with certainty
   from general knowledge (e.g., Paris is in France), you may include it, but
   for less-certain administrative divisions, include the descriptive reference
   verbatim (e.g., 'Andrew Johnson National Historic Site Greeneville county')
   without fabricating the county name.

   **Constraint-first disambiguation within the SAME hop**: When a hop
   describes an entity by a specific attribute or accomplishment (e.g.,
   'the emperor under whom the empire reached its greatest extent',
   'the dynasty under which porcelain flourished'), you MUST identify ALL
   candidates that satisfy that specific constraint BEFORE selecting the
   answer — do NOT substitute the most famous holder of the title/role.
   For empires/dynasties with multiple rulers, the constraint is the
   distinguishing key: 'greatest extent' uniquely identifies Trajan for the
   Roman Empire, 'cultural golden age' may identify a different emperor than
   'military conquest'. List 2-3 candidates for the role, then select the one
   matching the constraint. If unsure, include the constraint verbatim as a
   search term alongside your best candidate name (e.g., 'Roman Empire
   greatest extent Trajan' not 'Roman Empire greatest extent Augustus').
   **Never substitute a famous ruler for a less-famous one when the constraint
   distinguishes them** — Augustus founded the Roman Empire but Trajan
   expanded it to its greatest extent; these are different facts and different
   emperors.

   **Geographic entity resolution fallback**: When a hop requires identifying
   a specific place (country, state, city) by a factual description (e.g.,
   'the state that received error coins', 'the country where X was born'),
   and you cannot confidently identify the exact place, you MUST include
   MULTIPLE plausible candidates as search terms rather than a single guess.
   For US states, include the most plausible 2-3 candidate state names
   alongside the descriptive reference — e.g., 'error coins state Tennessee
   Wisconsin' rather than just 'error coins state Wisconsin'. This ensures
   lexical retrieval can match the correct state document even if your
   single-guess is wrong. NEVER resolve a geographic-by-description hop to
   only one entity name when you are uncertain — the cost of a wrong name
   is total retrieval failure for that hop's document.

   **Cultural-default bias guard** (MANDATORY): When a query references an
   unnamed entity by description only (e.g., 'a philosopher first described
   evaporation', 'the administration of the Yuan dynasty'), do NOT default to
   the most culturally familiar candidate (e.g., assuming Aristotle or ancient
   Greece for any unnamed philosopher). Instead, examine ALL contextual clues
   in the full query: if the query mentions a specific dynasty, region, or
   historical period (e.g., 'Yuan dynasty'), the unnamed entity likely belongs
   to that same cultural/historical sphere. A Chinese dynasty reference
   strongly implies the philosopher is Chinese; do NOT substitute a Western
   philosopher. When the cultural context is ambiguous, keep the descriptive
   reference verbatim (e.g., 'philosopher first described evaporation') AND
   include the contextual domain term (e.g., 'Han dynasty', 'China') inferred
   from the query's explicit entities — never substitute a name from a
   different cultural tradition.

   **Same-tradition default bias guard** (MANDATORY): Even within the same
   cultural tradition, do NOT substitute the most famous entity for a
   less-famous one when the query's constraint distinguishes them. E.g.,
   'the Roman emperor under whom the empire reached its greatest extent' =
   Trajan (NOT Augustus, who founded the principate but did not reach greatest
   extent); 'the Chinese dynasty under which porcelain peaked' = Song (NOT
   Tang, which is more famous but known for poetry, not porcelain). When
   multiple candidates exist within the same tradition, the query's
   qualifying constraint (greatest extent, golden age, specific achievement)
   is the disambiguator — use it. If you cannot determine which candidate
   matches the constraint, include BOTH the constraint verbatim AND your best
   candidate name (e.g., 'Roman Empire emperor greatest extent Trajan').

   **Disambiguation rule for titles/works**: When the query mentions a film,
   book, or other creative work by a short or ambiguous title (e.g.,
   "Marooned"), you MUST check which entity the query's context refers to
   before adding country or creator information. If the query constrains the
   work to a specific country or region (e.g., "filming Marooned in the birth
   country of Uday Hussein's mother" constrains the film to Iraq or the
   broader Middle East), prefer the work associated with that constrained
   region over the most-famous同名 work. NEVER assume a Western/English-language
   work when the query's geographic constraints point elsewhere. If unsure
   which work is meant, include BOTH candidates' distinguishing terms (e.g.,
   "Marooned in Iraq Bahman Ghobadi Iranian film | Marooned 1969 American
   film") so the recall system can find documents for either.

   **Biographical identity resolution is especially error-prone**: when a
   query refers to "the director of [film]", "the author of [book]", or
   similar role-by-work references, the named individual may not be widely
   known and LLM guesses are frequently wrong. When resolving such references,
   ALWAYS include the descriptive role alongside the name (e.g., "the director
   of The Man from Morocco Mutz Greenbaum") so that if the name is wrong, the
   descriptive role still enables retrieval. **Verification requirement**: after
   writing each hop sub-query referencing a role-by-work descriptor, confirm
   that a proper noun (person/group name) appears alongside the role. A
   sub-query containing only 'director', 'author', or 'performer' without
   a proper noun is a FAILED rewrite — documents about the person contain
   the person's name, not the role word. If you cannot resolve the name,
   include the work title verbatim as a proxy (e.g., 'La Modelo de la calle
   Florida film director Julio Irigoyen Argentine tango') so that terms
   co-occurring with the director name in documents are still searchable.

   **Critical safeguard — never substitute away the original descriptor**:
   Whenever you infer a proper noun, ALSO include the original descriptive
   reference verbatim from the query as a separate sub-query term. For example,
   if you infer 'Marcel Varnel' as the director of The Man from Morocco, output
   BOTH 'Marcel Varnel' AND 'director of The Man from Morocco film' as separate
   search terms — never replace the descriptive reference with the inferred
   name alone. This ensures retrieval can still find the document using the
   original descriptor even if the inferred name is wrong.
   **Context-anchored descriptor expansion**: When a descriptor contains an
   ambiguous term whose famous referent differs from the context-specific
   referent, the sub-query containing the original descriptor MUST also include
   the already-resolved context entity to anchor retrieval to the correct
   referent's documents. For example, if hop 1 resolved 'Las Vegas' and hop 2
   references 'the gold spike', the descriptor sub-query must be 'gold spike
   owner Las Vegas' (anchoring to Las Vegas) not just 'gold spike owner'
   (which retrieves the famous railroad ceremony). This anchoring is what
   allows BM25 to differentiate the context-specific referent from the globally
   famous one even when the resolved entity name is wrong.
   **Additionally, each sub-query MUST preserve the original query's role
   descriptors (e.g., 'creator', 'painter', 'performer', 'capital',
   'birthplace') alongside any resolved proper nouns** — dropping the
   descriptor 'creator of paintings' and replacing it with only the resolved
   city name loses the lexical retrieval signal for documents about the
   creator. A sub-query like 'paintings Kuwait City birthplace' misses
   the 'creator' and 'painter' keywords that bridge documents (about the
   painter's biography) actually contain. Always include both: the resolved
   proper noun AND the original descriptive role (e.g., 'creator painter
   paintings Kuwait Nabil Kanso Kuwait City birthplace Shurooq Amin').
10. Do NOT add generic synonyms or tangentially related terms (e.g., if the
    query is about Appalachian and Rocky Mountains, do not add "Sierra Nevada"
    or "Cascade Range" — only terms that directly help retrieve the specific
    hops). Synonyms are acceptable only when they are direct aliases for a
    resolved entity (e.g., "Great Smoky Mountains" for part of the Appalachians)
    or for entities explicitly named in the query.
11. Do not answer the question — only produce search queries.
12. OUTPUT FORMAT (CRITICAL): Your entire response is used verbatim as the
    search query. You MUST output ONLY the rewritten query text — no
    step-by-step reasoning, no numbered hop explanations, no markdown
    formatting, no introductory text, no conclusions, and no answers. Any
    reasoning or explanation text you emit becomes part of the search query
    and degrades retrieval quality. Do your reasoning silently; output only
    the pipe-separated sub-queries. Example output for a 3-hop question:
    United Kingdom Brexit European Union | Apache helicopter base United Kingdom Army Air Corps | United Kingdom driving test introduction
13. **Include alternate surface forms for entity names**: when a resolved
    entity has common alternate names used in documents (e.g., "Britain" for
    "United Kingdom", "UK" for "United Kingdom"), include those alternate
    surface forms alongside the primary name in the relevant sub-query so
    that both lexical and semantic retrieval can match the golden document's
    wording. This applies to country names, organization names, and other
    entities with well-known aliases — it does NOT mean adding generic
    synonyms.
14. **Include organizational and institutional names from the reasoning
    chain**: when a hop identifies a military unit, government body, or other
    institution (e.g., "Army Air Corps", "British Army", "16 Air Assault
    Brigade"), include those names in the sub-query even if the original query
    describes the entity by function (e.g., "apache helicopters"). Bridge
    documents often name the institution rather than the equipment.
15. **Educational institution abbreviations**: when a hop resolves to an
    educational institution (university, institute, academy), ALWAYS include
    both the full official name AND any well-known abbreviation or acronym
    (e.g., "Gerasimov Institute of Cinematography VGIK", "Massachusetts
    Institute of Technology MIT", "London School of Economics LSE").
    Documents frequently reference institutions by abbreviation only — a
    bridge document about another alumnus may contain only "VGIK" without the
    full name. Resolving a university hop without its abbreviation misses
    documents that share the institution as a bridge entity but only use the
    abbreviation. Include the abbreviation as a standalone token in the
    sub-query so both BM25 and dense retrieval can match it.

The `{query}` slot is substituted by the `query_rewrite` node.

Original query: {query}

Rewritten query: