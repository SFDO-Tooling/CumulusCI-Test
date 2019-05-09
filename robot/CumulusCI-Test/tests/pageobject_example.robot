*** Settings ***
Resource  cumulusci/robotframework/Salesforce.robot
Library   cumulusci.robotframework.PageObjects
...  robot/CumulusCI-Test/resources/ContactPages.py

Suite Setup     Run keywords
...  Open test browser
...  AND  Create test data
Suite Teardown  Delete Records and Close Browser

*** Keywords ***
Create test data
    Salesforce insert  Contact
    ...  FirstName=Inigo
    ...  LastName=Montoya

*** Test cases ***
Example of using a page object

    # We haven't defined a home page object for Contacts, so this will
    # use a dynamically generated home page object
    Go to page  Home  Contact

    # We know that the Contact home page redirects to the listing
    # page. We can use 'Current Page Should Be' to verify we are
    # on the expected page, and load the keywords for that page.
    Current page should be  Listing  Contact

    # Our custom page object defines the keyword "Click Contact Link"
    Click Contact Link  Inigo Montoya

    # After clicking the link, we should be on the detail page
    # for the given contact.
    Current page should be  Detail  Contact  firstName=Inigo  lastName=Montoya
    