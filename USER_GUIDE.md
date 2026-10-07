# How to use Hopper

There are two parts to Hopper: an interface that lets you submit queries to the agent (the query interface) and an interface to configure Hopper (the admin interface). There's also a Knowledge Base page that shows which documents Hopper is using.

## Signing in

Go to Hopper's address and sign in with your username or email address and your password. Accounts are created by an administrator. If you forgot your password, click **Forgot your password?** on the sign-in page and follow the link sent to your email.

After you log in for the first time, you will need to accept the Terms of Use by clicking **I Agree to the Terms of Use**. After you have done this, you will be redirected to the query interface. If the terms change, you will be asked to accept the new version the next time you use Hopper. You can review the terms at any time from the user menu (see below).

## The Query Interface

The query interface is fairly simple and behaves like most chat bots. Type your question into the "Ask me something..." input field at the bottom of the screen and press Enter or click the send button. While the agent is working, you will see "Thinking..."; answers can take up to a couple of minutes.

### Conversations

- Click the **New Chat +** button in the sidebar on the left to start a new conversation. If you type a question without starting a new chat, it is added to your most recent conversation.
- Your ten most recent conversations are listed in the sidebar below the button. Click one to go back to it. A conversation is named after its first question.
- Once you have an active conversation or go back to a previous one, you can see all the questions you asked. To jump to an earlier question, click the **Question History** button (the list icon) in the top right corner and select the question.
- Your conversations are only visible to you.

### Reading an answer

Answers are shown as a list of documents from the knowledge base that are relevant to your question, for example "3 documents found". Each result shows:

- a **PDF** or **Website** label,
- the **title**, which links to the document (PDFs open in a new tab and require you to be signed in),
- the **publisher**, if known. Results from State publishers are listed first,
- a **summary** of what the document says about your question and why it is **relevant**,
- for PDFs, a **Relevant pages** section listing the pages that matched, each with an excerpt. Click a page number to open the PDF at that page. Page numbers mentioned in the summary are also clickable.

If nothing relevant is found, you will see "No documents were found." Try rephrasing your question. If something goes wrong, you will see "Something went wrong. Please try again."

### The user menu

Click your name in the bottom left corner to open the user menu. From there you can:

- view the **Terms of Use** and whether you have accepted them,
- **Delete All Conversations**. You will be asked to confirm, and this can't be undone,
- **Sign Out**.

## The Knowledge Base Page

Click the **Knowledge Base** button in the bottom left corner to see the websites and PDFs that Hopper knows about, on the **Websites** and **PDFs** tabs. Click **Back to Chat** to return to the query interface.

Click **Compare with Knowledge Base** to check whether Hopper's list matches what is actually in the knowledge base that the agent searches. After the comparison:

- each resource's **KB Status** shows **In Sync** or **Missing from KB**, and a summary shows the totals;
- documents that are in the knowledge base but not listed in Hopper appear under "Documents in KB but not tracked internally".

Depending on your permissions, you will also see these buttons:

- **Add to KB**: sends a resource that is missing from the knowledge base to it again.
- **Track in Hopper**: adds an untracked knowledge base document to Hopper's list. For PDFs, Hopper also downloads a copy of the file.
- **Remove from KB**: deletes an untracked document from the knowledge base.

## The Admin Interface

If a user has access to the admin interface, they will see a button "Admin" in the bottom left corner. Clicking on that button will open the admin interface in a new tab. Depending on the level of access a user has, they will see different parts of the admin interface.

### Managing users

Go to "Users" and click "Add user" to create an account with a username, email address and password. To give someone access to the admin interface, check **Staff status** on their user page and give them permissions, either directly or by adding them to a group. For example, a "curator" group can have the permissions to add, change, delete and view PDF and website resources and the document metadata lists. **Superuser status** gives full access.

### Configuring SIM workflows

Which SIM workflow should be used to talk to an agent can be configured using the "Sim workflows" option. The URL to execute a workflow can be retrieved from the SIM installation. Add it to the "Agent endpoint" field. To activate the workflow, check the "Is active" checkbox and save the workflow. You can also select a workflow in the list and use the action "Set selected workflow as active".

Only one workflow can be active at a time: activating a workflow deactivates the previously active one. You can't deactivate or delete the active workflow until another one has been activated. Changes take effect for the next question that is asked.

### Adding Documents

To add documents, go to "PDF Resources" or "Website Resources". When you save a resource, Hopper sends it to the knowledge base (HopperMCP) in the background. The **Status** column shows the progress; refresh the page to update it:

- **Processing**: the resource is being sent to the knowledge base.
- **Success**: the resource is in the knowledge base.
- **Warning**: the knowledge base is still processing the file, which is common for large PDFs. **Do not upload it again**, because that would create a duplicate. Check again in a few minutes.
- **Error**: the upload failed. The status message says why.

Each resource can have optional metadata: **Date published** (a year, `YYYY-MM` or `YYYY-MM-DD`), **Document type**, **Document author institution**, **Institution type** and **Publisher**. The publisher is shown with the resource in answers, and resources whose publisher is `State` are listed first.

When uploading a web resource, Hopper will send the request to HopperMCP, which will attempt to retrieve the website page and then add it to the knowledge base. If the webpage disallows bots, or the URL redirects to another address, this might fail; use the page's final address. Saving a website again refreshes its content in the knowledge base.

PDFs can be uploaded individually (click "Add PDF Resource") or as a zip file containing multiple files (click "Upload zip of PDFs" on the PDF Resources list). Note that saving changes to an existing PDF resource sends the file to the knowledge base again as a new document.

When uploading a zip file, the zip file has to contain exactly one CSV file (for example `metadata.csv`) with one row per PDF. The `filename` and `title` columns are required; the other columns are optional and can be left empty:

```
filename,title,Document Type,Document Author Institution,Publisher,Institution Type,Date Published
```

- `filename` is the name of the PDF inside the zip file.
- Document types, author institutions and institution types that don't exist yet are created automatically.
- Rows are skipped, with a warning, if the file is not in the zip file, if the filename or title is missing, or if a PDF with the same filename and title has already been uploaded.
- An invalid date is left empty, with a warning; the rest of the row is still imported.

### Document metadata lists

The lists of choices for **Document Types**, **Document Author Institutions** and **Institution Types** can be edited in the admin interface. To add many values at once, click **Import CSV** on the list page and upload a CSV file with one name per row. A first row containing `name` is treated as a header, and duplicates are skipped.

### Deleting documents

Deleting a PDF or website resource in the admin interface also removes it from the knowledge base, so the agent will no longer find it. If the knowledge base can't be reached, the resource is kept and an error is shown; try again later.

### Other admin sections

- **Conversations** and **Q&A Records**: all questions and answers, for review and troubleshooting.
- **Terms acceptances**: a read-only record of which user accepted which version of the Terms of Use, and when.
- **OpenID Connect IdP → Clients**: credentials that other services (the knowledge base and the SIM agent) use to connect to Hopper. See [DOCUMENTATION.md](DOCUMENTATION.md#first-time-setup).
