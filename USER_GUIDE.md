# How to use Hopper

There are parts to Hopper: an interface that lets you submit queries to the agent (the query interface) and an interface to configure Hopper (the admin interface). 

## The Query Interface

After you login for the first time, you will need to accept the terms and conditions. After you have done this, you will be redirected to the query interface.

The query interface is fairly simple and behaves like most chat bots. You can create new conversations using the "New Chat+" button on the left and type your queries into the "Ask me somthing..." input field at the bottom of the screen. Once you have an active conversation or go back to a previous conversation (using the listed conversations on the left), you can see all the questions you asked and scroll to them using the menu in top right corner.

The menu in the bottom left corner will allow you to delete all previous conversations and to sign out.

## The Admin Interface

If a user has access to the admin interface, they will see a button "Admin" in the bottom left corner. Clicking on that button will open the admin interface. Depending on the level of access a user has, they will see different parts of the admin interface.

### Configuring SIM workflows

Which SIM workflow should be used to talk to an agent can be configured using the "SIM Workflows" option. The url to execute a workflow can be retrieved from the SIM installation. It then you need to add them into the "Agent endpoint" field. To activate the workflow check the "Is active" checkbox and save the workflow.

### Adding Documents

To add documents, go to "PDF Resources" or "Website Resources". PDFs can be uploaded as zip file containing multiple files or individually. When uploading a zip file, the zip file has to contain a csv file named `metadata.csv` with the following columns:

```
filename,title,Document Type,Document Author Institution,Publisher,Institution Type,Date Published
```

When uploading a web resource, Hopper will send the request to HopperMCP, which will attempt to retrieve the website page and then add it to the knowledge base. If the webpage disallows bots this might fail.
